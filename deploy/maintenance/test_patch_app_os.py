"""Prevent mutable tags or images from another source entering OS activation."""

import unittest

import patch_app_os as patch


class ImageGuards(unittest.TestCase):
    def test_requires_immutable_image_and_exact_revision(self):
        image = "sha256:" + "a" * 64
        sha = "b" * 40
        info = {
            "Id": image,
            "Config": {"Labels": {"org.opencontainers.image.revision": sha}},
        }
        patch.validate_image(info, image, sha)
        for candidate, source, observed in (
            ("latest", sha, info),
            (image, "c" * 40, info),
            (image, sha, {"Id": image, "Config": {"Labels": {}}}),
            (image, sha, {**info, "Id": "sha256:" + "d" * 64}),
        ):
            with (
                self.subTest(candidate=candidate, source=source),
                self.assertRaises(ValueError),
            ):
                patch.validate_image(observed, candidate, source)
