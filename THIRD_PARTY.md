# Third-party components

## DejaVu Fonts (app/assets/fonts/)

Files: `DejaVuSans.ttf`, `DejaVuSans-Bold.ttf`, `DejaVuSans-Oblique.ttf`,
`LICENSE` — taken unmodified from the dejavu-fonts 2.37 release
(<https://dejavu-fonts.github.io/>). Bundled so PDF export can render
real Unicode text (the FPDF core fonts are latin-1 only).

License: the DejaVu license (a permissive, public-domain-style license
built on the Bitstream Vera license — see `app/assets/fonts/LICENSE`)
permits redistribution in unmodified form, which is exactly how the
files ship here. DejaVu Sans covers Latin, Latin Extended, Greek,
Cyrillic, Hebrew, Georgian, Armenian and common symbols; CJK, Thai and
most Indic scripts are outside its glyph coverage and render as
missing-glyph boxes in exported PDFs.
