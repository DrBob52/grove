"""Tests for scripts/imageinfo.py."""

import unittest

import helpers
import imageinfo


class PngInfoTests(unittest.TestCase):
    def test_size_and_kind(self) -> None:
        info = imageinfo.read_image_info(helpers.make_png(37, 21))
        self.assertEqual((info.kind, info.width, info.height), ("png", 37, 21))

    def test_alpha_by_color_type(self) -> None:
        expected = {0: False, 2: False, 3: False, 4: True, 6: True}
        for color_type, has_alpha in expected.items():
            with self.subTest(color_type=color_type):
                info = imageinfo.png_info(helpers.make_png(4, 4, color_type))
                self.assertEqual(info.has_alpha, has_alpha)

    def test_trns_chunk_counts_as_alpha(self) -> None:
        for color_type in (0, 2, 3):
            with self.subTest(color_type=color_type):
                info = imageinfo.png_info(helpers.make_png(4, 4, color_type, trns=True))
                self.assertTrue(info.has_alpha)

    def test_large_dimension(self) -> None:
        info = imageinfo.png_info(helpers.make_png(2048, 2, 6))
        self.assertEqual((info.width, info.height), (2048, 2))

    def test_truncated_or_bad_headers_return_none(self) -> None:
        png = helpers.make_png(8, 8)
        self.assertIsNone(imageinfo.png_info(png[:20]))
        self.assertIsNone(imageinfo.png_info(png[:8]))
        self.assertIsNone(imageinfo.png_info(b""))
        self.assertIsNone(imageinfo.png_info(png.replace(b"IHDR", b"XXXX", 1)))

    def test_zero_dimension_is_unreadable(self) -> None:
        self.assertIsNone(imageinfo.png_info(helpers.make_png(0, 5)))

    def test_signature_helper(self) -> None:
        self.assertTrue(imageinfo.has_png_signature(helpers.make_png(1, 1)))
        self.assertFalse(imageinfo.has_png_signature(b"\x89PNG\r\n\x1a"))
        self.assertFalse(imageinfo.has_png_signature(b"GIF89a-not-a-png"))


class JpegInfoTests(unittest.TestCase):
    def test_sof0(self) -> None:
        info = imageinfo.read_image_info(helpers.make_jpeg(640, 480))
        self.assertEqual((info.kind, info.width, info.height, info.has_alpha), ("jpeg", 640, 480, False))

    def test_skips_other_segments_before_sof(self) -> None:
        info = imageinfo.jpeg_info(helpers.make_jpeg(300, 200, app0=True))
        self.assertEqual((info.width, info.height), (300, 200))

    def test_every_sof_marker_is_recognised(self) -> None:
        for marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            with self.subTest(marker=hex(marker)):
                info = imageinfo.jpeg_info(helpers.make_jpeg(50, 60, marker))
                self.assertEqual((info.width, info.height), (50, 60))

    def test_dht_dac_and_jpg_markers_are_not_frame_headers(self) -> None:
        for marker in (0xC4, 0xC8, 0xCC):
            with self.subTest(marker=hex(marker)):
                data = b"\xff\xd8" + helpers.jpeg_segment(marker, b"\x00" * 9) + helpers.make_jpeg(70, 80)[2:]
                info = imageinfo.jpeg_info(data)
                self.assertEqual((info.width, info.height), (70, 80))

    def test_fill_bytes_before_marker(self) -> None:
        data = helpers.make_jpeg(9, 7)
        data = data[:2] + b"\xff\xff" + data[2:]
        self.assertEqual(imageinfo.jpeg_info(data).width, 9)

    def test_no_frame_header_returns_none(self) -> None:
        self.assertIsNone(imageinfo.jpeg_info(b"\xff\xd8\xff\xd9"))
        self.assertIsNone(imageinfo.jpeg_info(b"\xff\xd8"))
        self.assertIsNone(imageinfo.jpeg_info(b"\xff\xd8" + helpers.jpeg_segment(0xDA, b"\x00\x01")))

    def test_truncated_sof_returns_none(self) -> None:
        self.assertIsNone(imageinfo.jpeg_info(helpers.make_jpeg(10, 10)[:8]))


class SniffingTests(unittest.TestCase):
    def test_unknown_data_returns_none(self) -> None:
        self.assertIsNone(imageinfo.read_image_info(b"RIFF....WEBPVP8 "))
        self.assertIsNone(imageinfo.read_image_info(b""))


if __name__ == "__main__":
    unittest.main()
