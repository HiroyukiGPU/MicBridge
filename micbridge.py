#!/usr/bin/env python3
"""MicBridge: send a Windows microphone to a macOS virtual audio device over LAN."""

from __future__ import annotations

import array
import math
import queue
import socket
import struct
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

try:
    import sounddevice as sd
except ImportError as exc:  # Friendly message when launched outside a terminal.
    raise SystemExit(
        "sounddevice がありません。先に 'python -m pip install -r requirements.txt' を実行してください。"
    ) from exc


APP_NAME = "MicBridge"
APP_VERSION = "2.1"
MAGIC_V1 = b"MB01"
MAGIC_V2 = b"MB02"
LEGACY_HEADER = struct.Struct("!4sI")
HEADER = struct.Struct("!4sIBH")  # magic, sequence, channels, frames
ACK_MAGIC = b"MBAK"
ACK_HEADER = struct.Struct("!4sI")
SAMPLE_RATE = 48_000
BLOCK_FRAMES = 240  # 5 ms; stereo packets remain below a typical 1500-byte MTU
SAMPLE_WIDTH = 2
DEFAULT_PORT = 50_000


def encode_packet(sequence: int, channels: int, audio: bytes) -> bytes:
    frames = len(audio) // (channels * SAMPLE_WIDTH)
    return HEADER.pack(MAGIC_V2, sequence, channels, frames) + audio


def decode_packet(packet: bytes) -> tuple[int, int, int, bytes, str] | None:
    """Decode v2 plus both previously shipped MB01 packet layouts."""
    if len(packet) >= HEADER.size and packet[:4] == MAGIC_V2:
        _magic, sequence, channels, frames = HEADER.unpack_from(packet)
        expected = HEADER.size + frames * channels * SAMPLE_WIDTH
        if channels in (1, 2) and frames > 0 and len(packet) == expected:
            return sequence, channels, frames, packet[HEADER.size:], "v2"
        return None

    if len(packet) >= LEGACY_HEADER.size and packet[:4] == MAGIC_V1:
        _magic, sequence = LEGACY_HEADER.unpack_from(packet)
        if len(packet) >= LEGACY_HEADER.size + 1:
            channels = packet[LEGACY_HEADER.size]
            audio = packet[LEGACY_HEADER.size + 1:]
            if channels in (1, 2) and len(audio) == 960 * channels * SAMPLE_WIDTH:
                return sequence, channels, 960, audio, "v1.1"
        audio = packet[LEGACY_HEADER.size:]
        if len(audio) == 960 * SAMPLE_WIDTH:
            return sequence, 1, 960, audio, "v1.0"
    return None


def convert_channels(audio: bytes, source: int, target: int) -> bytes:
    if source == target:
        return audio
    samples = array.array("h")
    samples.frombytes(audio)
    converted = array.array("h")
    if source == 1 and target == 2:
        for sample in samples:
            converted.extend((sample, sample))
    elif source == 2 and target == 1:
        for index in range(0, len(samples), 2):
            converted.append((samples[index] + samples[index + 1]) // 2)
    else:
        raise ValueError("対応していないチャンネル変換です")
    return converted.tobytes()


def audio_devices(kind: str, channels: int) -> list[tuple[int, str]]:
    """Return (PortAudio index, display name) for input or output devices."""
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    devices = []
    for index, device in enumerate(sd.query_devices()):
        if int(device[key]) >= channels:
            host = sd.query_hostapis(device["hostapi"])["name"]
            devices.append((index, f"{device['name']} — {host}"))
    return devices


def audio_devices_with_fallback(
    kind: str, channels: int
) -> tuple[int, list[tuple[int, str]]]:
    """Return compatible devices, falling back to mono when stereo is unavailable."""
    devices = audio_devices(kind, channels)
    if channels == 2 and not devices:
        mono_devices = audio_devices(kind, 1)
        if mono_devices:
            return 1, mono_devices
    return channels, devices


def audio_level_percent(audio: bytes) -> float:
    """Convert signed 16-bit PCM to a useful 0-100 dBFS-style meter value."""
    if not audio:
        return 0.0
    samples = array.array("h")
    samples.frombytes(audio)
    if not samples:
        return 0.0
    mean_square = sum(sample * sample for sample in samples) / len(samples)
    if mean_square <= 0:
        return 0.0
    dbfs = 20.0 * math.log10(math.sqrt(mean_square) / 32768.0)
    return max(0.0, min(100.0, (dbfs + 60.0) * (100.0 / 60.0)))


class Sender:
    def __init__(
        self, device: int, host: str, port: int, channels: int, status, connection, level
    ):
        self.device = device
        self.host = host
        self.port = port
        self.channels = channels
        self.status = status
        self.connection = connection
        self.level = level
        self.stop_event = threading.Event()
        self.audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=12)
        self.thread: threading.Thread | None = None
        self.stream = None

    def start(self):
        target_ip = socket.gethostbyname(self.host)
        self.connection("接続待ち")
        self.thread = threading.Thread(target=self._send_loop, args=(target_ip,), daemon=True)
        self.thread.start()

        def callback(indata, frames, _time_info, status):
            if status:
                self.status(f"入力警告: {status}")
            packet = bytes(indata)
            self.level(audio_level_percent(packet))
            try:
                self.audio_queue.put_nowait(packet)
            except queue.Full:
                try:
                    self.audio_queue.get_nowait()
                    self.audio_queue.put_nowait(packet)
                except queue.Empty:
                    pass

        self.stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_FRAMES,
            device=self.device,
            channels=self.channels,
            dtype="int16",
            callback=callback,
        )
        self.stream.start()
        self.status(f"送信中 → {target_ip}:{self.port}")

    def _send_loop(self, target_ip: str):
        sequence = 0
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        last_ack = 0.0
        connected = False
        try:
            while not self.stop_event.is_set():
                try:
                    audio = self.audio_queue.get(timeout=0.1)
                except queue.Empty:
                    audio = None
                if audio is not None:
                    sock.sendto(encode_packet(sequence, self.channels, audio), (target_ip, self.port))
                    sequence = (sequence + 1) & 0xFFFFFFFF

                while True:
                    try:
                        acknowledgement, address = sock.recvfrom(ACK_HEADER.size)
                    except BlockingIOError:
                        break
                    except OSError:
                        return
                    if (
                        len(acknowledgement) == ACK_HEADER.size
                        and ACK_HEADER.unpack(acknowledgement)[0] == ACK_MAGIC
                        and address[0] == target_ip
                        and address[1] == self.port
                    ):
                        last_ack = time.monotonic()
                        if not connected:
                            connected = True
                            self.connection(f"接続済み  {target_ip}:{self.port}")

                if connected and time.monotonic() - last_ack > 2.0:
                    connected = False
                    self.connection("接続が途切れました（再接続待ち）")
        finally:
            sock.close()

    def stop(self):
        self.stop_event.set()
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
        if self.thread is not None:
            self.thread.join(timeout=1)


class Receiver:
    def __init__(
        self, device: int, port: int, channels: int, status, connection, level
    ):
        self.device = device
        self.port = port
        self.channels = channels
        self.silence = bytes(BLOCK_FRAMES * channels * SAMPLE_WIDTH)
        self.status = status
        self.connection = connection
        self.level = level
        self.stop_event = threading.Event()
        self.play_queue: queue.Queue[bytes] = queue.Queue(maxsize=16)
        self.thread: threading.Thread | None = None
        self.stream = None
        self.sock: socket.socket | None = None
        self.expected_sequence: int | None = None
        self.play_ready = threading.Event()
        self.last_packet_at = 0.0
        self.pending_audio = bytearray()
        self.packet_count = 0

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", self.port))
        self.sock.settimeout(0.25)
        self.thread = threading.Thread(target=self._receive_loop, daemon=True)
        self.thread.start()
        self.connection("接続待ち")

        def callback(outdata, frames, _time_info, status):
            if status:
                self.status(f"出力警告: {status}")
            if not self.play_ready.is_set():
                audio = self.silence
            else:
                try:
                    audio = self.play_queue.get_nowait()
                except queue.Empty:
                    self.play_ready.clear()
                    audio = self.silence
            outdata[:] = audio if len(audio) == len(outdata) else self.silence

        self.stream = sd.RawOutputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_FRAMES,
            device=self.device,
            channels=self.channels,
            dtype="int16",
            callback=callback,
        )
        self.stream.start()
        addresses = local_ipv4_addresses()
        shown = ", ".join(addresses) if addresses else "このPCのIP"
        self.status(f"受信待機中  {shown}:{self.port}")

    def _enqueue(self, audio: bytes):
        try:
            self.play_queue.put_nowait(audio)
        except queue.Full:
            try:
                self.play_queue.get_nowait()
                self.play_queue.put_nowait(audio)
            except queue.Empty:
                pass
        if self.play_queue.qsize() >= 6:
            self.play_ready.set()

    def _append_audio(self, audio: bytes, source_channels: int):
        self.pending_audio.extend(convert_channels(audio, source_channels, self.channels))
        chunk_size = BLOCK_FRAMES * self.channels * SAMPLE_WIDTH
        while len(self.pending_audio) >= chunk_size:
            self._enqueue(bytes(self.pending_audio[:chunk_size]))
            del self.pending_audio[:chunk_size]

    def _receive_loop(self):
        peer = None
        protocol = ""
        while not self.stop_event.is_set():
            try:
                packet, address = self.sock.recvfrom(65_535)  # type: ignore[union-attr]
            except socket.timeout:
                if self.last_packet_at and time.monotonic() - self.last_packet_at > 2.0:
                    peer = None
                    self.last_packet_at = 0.0
                    self.connection("接続が途切れました（再接続待ち）")
                continue
            except OSError:
                break
            decoded = decode_packet(packet)
            if decoded is None:
                self.status(f"UDPは到着しましたが形式が不正です ← {address[0]} ({len(packet)} bytes)")
                continue
            sequence, source_channels, frames, audio, received_protocol = decoded
            self.last_packet_at = time.monotonic()
            try:
                self.sock.sendto(ACK_HEADER.pack(ACK_MAGIC, sequence), address)  # type: ignore[union-attr]
            except OSError:
                pass
            if peer != address:
                peer = address
                self.expected_sequence = sequence
                self.pending_audio.clear()
                self.connection(f"接続済み  {address[0]}")

            if self.expected_sequence is not None:
                gap = (sequence - self.expected_sequence) & 0xFFFFFFFF
                if 0 < gap < 4:
                    for _ in range(gap):
                        missing = bytes(frames * source_channels * SAMPLE_WIDTH)
                        self._append_audio(missing, source_channels)
                elif gap >= 0x80000000:  # old or reordered packet
                    continue
            self.level(audio_level_percent(audio))
            self._append_audio(audio, source_channels)
            self.expected_sequence = (sequence + 1) & 0xFFFFFFFF
            self.packet_count += 1
            if protocol != received_protocol or self.packet_count % 200 == 1:
                protocol = received_protocol
                self.status(
                    f"受信中 ← {address[0]}:{address[1]} / {protocol} / "
                    f"{source_channels}ch / {self.packet_count} packets"
                )

    def stop(self):
        self.stop_event.set()
        if self.sock is not None:
            self.sock.close()
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
        if self.thread is not None:
            self.thread.join(timeout=1)


def local_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if not address.startswith("127."):
                addresses.add(address)
    except socket.gaierror:
        pass
    # A UDP connect discovers the preferred LAN address without sending data.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
        if not address.startswith("127."):
            addresses.add(address)
    except OSError:
        pass
    finally:
        probe.close()
    return sorted(addresses)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("590x540")
        self.minsize(540, 510)
        self.worker: Sender | Receiver | None = None
        self.devices: list[tuple[int, str]] = []

        self.role = tk.StringVar(value="send")
        # Most built-in microphones expose a single input channel. Starting in
        # stereo used to filter all of them out and made the list look broken.
        self.channel_mode = tk.StringVar(value="ステレオ")
        self.device = tk.StringVar()
        self.host = tk.StringVar()
        self.port = tk.StringVar(value=str(DEFAULT_PORT))
        self.status_text = tk.StringVar(value="停止中")
        self.connection_text = tk.StringVar(value="未接続")
        self.level_value = tk.DoubleVar(value=0.0)
        self._latest_level = 0.0
        self._displayed_level = 0.0
        self._build_ui()
        self.refresh_devices()
        self.after(50, self._animate_level)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self):
        root = ttk.Frame(self, padding=20)
        root.pack(fill="both", expand=True)

        ttk.Label(root, text=f"PC間で音声を送受信  v{APP_VERSION}", font=("TkDefaultFont", 17, "bold")).pack(anchor="w")
        ttk.Label(root, text="2台を同じLANに接続してください。", foreground="#555").pack(anchor="w", pady=(2, 18))

        roles = ttk.Frame(root)
        roles.pack(fill="x", pady=(0, 12))
        ttk.Radiobutton(roles, text="このPCから送る", variable=self.role, value="send", command=self.refresh_devices).pack(side="left")
        ttk.Radiobutton(roles, text="このPCで受ける", variable=self.role, value="receive", command=self.refresh_devices).pack(side="left", padx=22)

        form = ttk.Frame(root)
        form.pack(fill="x")
        ttk.Label(form, text="音声デバイス", width=16).grid(row=0, column=0, sticky="w", pady=7)
        self.device_combo = ttk.Combobox(form, textvariable=self.device, state="readonly")
        self.device_combo.grid(row=0, column=1, sticky="ew", pady=7)
        ttk.Button(form, text="更新", command=self.refresh_devices).grid(row=0, column=2, padx=(8, 0))

        ttk.Label(form, text="受信側のIPアドレス", width=16).grid(row=1, column=0, sticky="w", pady=7)
        self.host_entry = ttk.Entry(form, textvariable=self.host)
        self.host_entry.grid(row=1, column=1, columnspan=2, sticky="ew", pady=7)

        ttk.Label(form, text="チャンネル", width=16).grid(row=2, column=0, sticky="w", pady=7)
        channel_combo = ttk.Combobox(form, textvariable=self.channel_mode, values=("モノラル", "ステレオ"), state="readonly", width=10)
        channel_combo.grid(row=2, column=1, sticky="w", pady=7)
        channel_combo.bind("<<ComboboxSelected>>", lambda _event: self.refresh_devices())

        ttk.Label(form, text="ポート", width=16).grid(row=3, column=0, sticky="w", pady=7)
        ttk.Entry(form, textvariable=self.port, width=10).grid(row=3, column=1, sticky="w", pady=7)
        form.columnconfigure(1, weight=1)

        indicators = ttk.Frame(root)
        indicators.pack(fill="x", pady=(14, 0))
        ttk.Label(indicators, text="接続状態", width=16).grid(row=0, column=0, sticky="w", pady=5)
        ttk.Label(indicators, textvariable=self.connection_text).grid(row=0, column=1, sticky="w", pady=5)
        ttk.Label(indicators, text="音声レベル", width=16).grid(row=1, column=0, sticky="w", pady=5)
        ttk.Progressbar(
            indicators,
            variable=self.level_value,
            maximum=100,
            mode="determinate",
        ).grid(row=1, column=1, sticky="ew", pady=5)
        indicators.columnconfigure(1, weight=1)

        self.start_button = ttk.Button(root, text="開始", command=self.toggle)
        self.start_button.pack(fill="x", pady=(20, 10), ipady=6)
        ttk.Separator(root).pack(fill="x", pady=8)
        ttk.Label(root, textvariable=self.status_text, wraplength=535).pack(anchor="w", pady=4)
        ttk.Label(root, text="送信側と受信側で同じチャンネル設定を選んでください。", foreground="#666").pack(anchor="w", pady=(8, 0))
        self._update_role_ui()

    def _update_role_ui(self):
        self.host_entry.configure(state="normal" if self.role.get() == "send" else "disabled")

    def refresh_devices(self):
        if self.worker is not None:
            return
        self._update_role_ui()
        kind = "input" if self.role.get() == "send" else "output"
        requested_channels = 2 if self.channel_mode.get() == "ステレオ" else 1
        try:
            channels, self.devices = audio_devices_with_fallback(kind, requested_channels)
            if channels != requested_channels:
                self.channel_mode.set("モノラル")
                self.status_text.set(
                    "ステレオ対応デバイスがないため、モノラルに切り替えました。"
                )
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"音声デバイスを取得できません。\n{exc}")
            self.devices = []
        names = [name for _, name in self.devices]
        self.device_combo["values"] = names
        if names:
            preferred = next(
                (
                    n for n in names
                    if "BlackHole" in n
                    or (kind == "output" and "CABLE Input" in n)
                ),
                names[0],
            )
            self.device.set(preferred)
        else:
            self.device.set("")

    def set_status(self, text: str):
        self.after(0, self.status_text.set, text)

    def set_connection(self, text: str):
        self.after(0, self.connection_text.set, text)

    def set_level(self, value: float):
        self._latest_level = value

    def _animate_level(self):
        target = self._latest_level
        if target >= self._displayed_level:
            self._displayed_level = target
        else:
            self._displayed_level *= 0.84
            if self._displayed_level < 0.5:
                self._displayed_level = 0.0
        self._latest_level *= 0.92
        self.level_value.set(self._displayed_level)
        self.after(50, self._animate_level)

    def toggle(self):
        if self.worker is not None:
            self.stop()
            return
        try:
            port = int(self.port.get())
            if not 1024 <= port <= 65535:
                raise ValueError("ポートは1024～65535で指定してください。")
            selection = self.device_combo.current()
            if selection < 0:
                raise ValueError("音声デバイスを選択してください。")
            device_index = self.devices[selection][0]
            channels = 2 if self.channel_mode.get() == "ステレオ" else 1
            if self.role.get() == "send":
                if not self.host.get().strip():
                    raise ValueError("受信側のIPアドレスを入力してください。")
                worker: Sender | Receiver = Sender(
                    device_index,
                    self.host.get().strip(),
                    port,
                    channels,
                    self.set_status,
                    self.set_connection,
                    self.set_level,
                )
            else:
                worker = Receiver(
                    device_index,
                    port,
                    channels,
                    self.set_status,
                    self.set_connection,
                    self.set_level,
                )
            self.worker = worker
            worker.start()
            self.start_button.configure(text="停止")
            self.device_combo.configure(state="disabled")
        except Exception as exc:
            if self.worker is not None:
                self.worker.stop()
                self.worker = None
            messagebox.showerror(APP_NAME, f"開始できませんでした。\n{exc}")

    def stop(self):
        if self.worker is not None:
            self.worker.stop()
            self.worker = None
        self.start_button.configure(text="開始")
        self.device_combo.configure(state="readonly")
        self.status_text.set("停止中")
        self.connection_text.set("未接続")
        self._latest_level = 0.0

    def on_close(self):
        self.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
