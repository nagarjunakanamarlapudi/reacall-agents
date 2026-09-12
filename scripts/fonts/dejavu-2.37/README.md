# Pinned presentation font

DejaVu Sans 2.37 regular and bold are bundled unchanged from the [upstream 2.37 release](https://github.com/dejavu-fonts/dejavu-fonts/releases/tag/version_2_37). The exact archive URL, per-font SHA-256 digests and license digest are in `manifest.json`. The renderer independently pins the two font digests and refuses missing or modified assets; it never searches operating-system fonts.

`LICENSE` preserves the complete upstream license, with trailing whitespace normalized. The Bitstream Vera and Arev notices permit redistribution with these notices retained; DejaVu changes are public domain. No font names or glyph data were modified. The upstream archive's notice also covers mathematical faces not bundled here.

`scripts/render_presentation.py` uses the locked Pillow version and explicit `ImageFont.Layout.BASIC`, avoiding optional host shaping-library selection. Both macOS and Ubuntu use these exact TTF bytes. Changing the font requires updating the digest pins, manifest, reviewed layout and committed presentation PNGs, followed by the real `scripts/render_diagrams.sh --verify` byte-parity check.

Pillow/zlib wheels can encode identical pixels differently across operating systems. Presentation PNGs therefore use an explicit canonical RGB PNG writer with stored DEFLATE blocks and fixed scanline/chunk ordering; no host compression routine selects the output bytes. This increases each 1920×1080 file to about 6.2 MB but preserves portable byte-for-byte parity without relaxing the check to pixel equality or existence.
