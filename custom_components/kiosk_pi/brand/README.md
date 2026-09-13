# Brand marks

A Raspberry Pi board inside a house, with three traces rising from it: the
house says where this runs, the board says what it drives, the traces are the
panels it reaches.

`source/logo-source.jpg` is the artwork **exactly as supplied**, and it is the
only thing here anyone should edit or replace. Every other file is derived
from it by `build.py` and committed, so the rasters are reproducible rather
than blobs nobody can diff.

## Files

| File | Size | Read by |
| --- | --- | --- |
| `source/logo-source.jpg` | 1024² | Nothing. It is the input. |
| `icon.png` | 256×256 | **The HACS brands check reads this exact path** (`<content path>/brand/icon.png` for a repository laid out under `custom_components/`), and core's `BrandsIntegrationView` serves it ahead of the CDN. |
| `icon@2x.png` | 512×512 | The `@2x` beside it. |
| `logo.png` / `logo@2x.png` | 418×512, 835×1024 | This repository's README on a light ground. |
| `dark_logo.png` / `dark_logo@2x.png` | 418×512, 835×1024 | The same lockup with the wordmark lifted for a dark ground. HACS renders the README inside a frontend that is dark by default, so this is the README's fallback `<img>`. |

**The icon is the mark alone, without the wordmark.** HACS and the sidebar
draw it small, and a 256px square asked to carry a two-line wordmark renders
the wordmark as grey mush.

## The page was off-white, not white

The source is a JPEG on a warm off-white page (~240/239/235). `build.py`
floods the page from the border only: the whites this mark draws on purpose --
the board, its ports, the trace nodes -- sit inside a closed blue outline, so
the flood never reaches them and there is no enclosed-white knockout. Every
opaque pixel bordering transparency then has its coverage solved against the
page colour from the nearest solid ink, because a luminance gate misses a
half-blue, half-page pixel and leaves a pale rim on anything dark.
