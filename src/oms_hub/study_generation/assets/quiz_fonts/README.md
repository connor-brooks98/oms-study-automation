# Quiz PDF fonts

Unmodified DejaVu Sans regular and bold, version 2.37, copied from the upstream
[release archive](https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip).
The archive's complete upstream `LICENSE` is included alongside the fonts
with trailing whitespace removed; license wording is unchanged.

SHA-256:

- Archive: `7576310b219e04159d35ff61dd4a4ec4cdba4f35c00e002a136f00e96a908b0a`
- DejaVuSans.ttf: `7da195a74c55bef988d0d48f9508bd5d849425c1770dba5d7bfc6ce9ed848954`
- DejaVuSans-Bold.ttf: `e6476c1b80502924294eed40894c5b18e06c181444ca953e5334262df9c27724`
- LICENSE: `f88d5294af5a772f9114eb385009e3478d62b21f3e5bbc34d159d328047c8867`

The files live inside the existing Hatch wheel package `src/oms_hub`. The renderer
loads these paths relative to its own module; Windows and macOS use identical
bundled bytes without OS font discovery, font installation or network access.
ReportLab embeds used TrueType subsets in each PDF. Keep the license with released
package/source artifacts. Neither font has been modified or renamed internally.

Every paragraph is checked against its selected font's actual cmap before any
export file is written. Unsupported characters raise ValueError with Unicode
code points; reviewed clinical text is never transliterated or replaced. This is
glyph coverage validation, not a claim to support every Unicode writing system.
Export format version 2 and an explicit PDF renderer version plus both loaded
font hashes bind the output folder, preserving former Helvetica/Symbol bundles.
