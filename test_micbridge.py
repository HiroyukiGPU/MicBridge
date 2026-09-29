import importlib.util
import pathlib
import unittest
from unittest import mock


MODULE_PATH = pathlib.Path(__file__).with_name("micbridge.py")


class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fake_sd = mock.MagicMock()
        with mock.patch.dict("sys.modules", {"sounddevice": fake_sd}):
            spec = importlib.util.spec_from_file_location("micbridge", MODULE_PATH)
            cls.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.module)

    def test_packet_size_is_safe_for_udp(self):
        size = self.module.HEADER.size + self.module.BLOCK_FRAMES * 2 * self.module.SAMPLE_WIDTH
        self.assertLess(size, 1_500)
        self.assertEqual(size, 971)

    def test_five_millisecond_block(self):
        self.assertEqual(self.module.BLOCK_FRAMES / self.module.SAMPLE_RATE, 0.005)

    def test_header_round_trip(self):
        audio = bytes(self.module.BLOCK_FRAMES * 2 * self.module.SAMPLE_WIDTH)
        packet = self.module.encode_packet(123, 2, audio)
        decoded = self.module.decode_packet(packet)
        self.assertEqual(decoded, (123, 2, self.module.BLOCK_FRAMES, audio, "v2"))

    def test_accepts_original_legacy_packet(self):
        audio = bytes(960 * self.module.SAMPLE_WIDTH)
        packet = self.module.LEGACY_HEADER.pack(self.module.MAGIC_V1, 7) + audio
        self.assertEqual(self.module.decode_packet(packet), (7, 1, 960, audio, "v1.0"))

    def test_accepts_stereo_v11_packet(self):
        audio = bytes(960 * 2 * self.module.SAMPLE_WIDTH)
        packet = self.module.LEGACY_HEADER.pack(self.module.MAGIC_V1, 8) + bytes((2,)) + audio
        self.assertEqual(self.module.decode_packet(packet), (8, 2, 960, audio, "v1.1"))

    def test_mono_to_stereo(self):
        mono = self.module.array.array("h", (100, -100)).tobytes()
        stereo = self.module.convert_channels(mono, 1, 2)
        values = self.module.array.array("h")
        values.frombytes(stereo)
        self.assertEqual(values.tolist(), [100, 100, -100, -100])

    def test_acknowledgement_round_trip(self):
        packet = self.module.ACK_HEADER.pack(self.module.ACK_MAGIC, 456)
        self.assertEqual(
            self.module.ACK_HEADER.unpack(packet),
            (self.module.ACK_MAGIC, 456),
        )

    def test_audio_level_meter(self):
        silence = b"\x00\x00" * 960
        loud = (20_000).to_bytes(2, "little", signed=True) * 960

        self.assertEqual(self.module.audio_level_percent(silence), 0.0)
        self.assertGreater(self.module.audio_level_percent(loud), 80.0)

    def test_stereo_device_lookup_falls_back_to_mono(self):
        mono_devices = [(1, "Built-in microphone — Core Audio")]
        with mock.patch.object(
            self.module,
            "audio_devices",
            side_effect=[[], mono_devices],
        ) as lookup:
            channels, devices = self.module.audio_devices_with_fallback("input", 2)

        self.assertEqual(channels, 1)
        self.assertEqual(devices, mono_devices)
        self.assertEqual(lookup.call_args_list, [mock.call("input", 2), mock.call("input", 1)])

    def test_mono_device_lookup_does_not_need_fallback(self):
        mono_devices = [(1, "Built-in microphone — Core Audio")]
        with mock.patch.object(
            self.module,
            "audio_devices",
            return_value=mono_devices,
        ) as lookup:
            channels, devices = self.module.audio_devices_with_fallback("input", 1)

        self.assertEqual(channels, 1)
        self.assertEqual(devices, mono_devices)
        lookup.assert_called_once_with("input", 1)


if __name__ == "__main__":
    unittest.main()
