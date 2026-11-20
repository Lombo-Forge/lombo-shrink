# The Lombo look

## Contents

- [The rule](#the-rule)
- [Colours and type](#colours-and-type)
- [The seal and the L](#the-seal-and-the-l)
- [LomboShrink's mark](#lomboshrinks-mark)
- [What every app needs](#what-every-app-needs)
- [What to avoid](#what-to-avoid)

## The rule

**The Lombo L is in every icon.** Each app has its own seal, and a small mark of its own sits in
the open corner of the L. Each app also gets one wordmark trick (Tier's letters step down,
Shrink's get smaller from S to K, Shelf's F leans).

## Colours and type

| Use | Value |
|---|---|
| Background | `#0d0a08` |
| Surface | `#1a140f` |
| Borders | `#2c231a`, `#443528` |
| Text, muted | `#f4ede3`, `#b3a696` |
| Ember gradient | `#ffb347` to `#ff6a1a` to `#e23d0f` |

Headings and wordmarks: Cinzel (SIL Open Font License). Body text: Geist.

## The seal and the L

`assets/icon.svg` is the base seal: a ring with the gradient, an inner frame line, and the L.
Copy it, keep the ring and the L, and add the app's mark to the open corner (right of the stem,
above the foot). Banners use the same seal on the left and the wordmark on the right.

## LomboShrink's mark

Two chevrons pointing at each other in the L's open corner (left one `#ffb347`, right one `#ff6a1a`, round
caps, stroke 7). The wordmark trick: the letters of SHRINK get smaller, 92 down to 42, on one baseline.
Files: `assets/icon.svg`, `banner.svg`, `social-preview.svg` and their PNGs, rendered with the command in the
workspace's `forge-branding` skill.

## What every app needs

- `assets/icon.svg`, plus a 512 px PNG for the container registry and GitHub.
- `assets/banner.svg`, shown at the top of the README (1200 x 300).
- A 1280 x 640 social preview image, uploaded in the repository settings.

SVG text uses fonts the viewer may not have, so export the PNG versions with the font installed
and use those where exact lettering matters.

## What to avoid

- No Tolkien names, scripts, rings or symbols. The feel comes from engraved capitals, a map-style
  frame and the forge colours, not from borrowed imagery.
- No logos, names or colours of other products. "For Jellyfin" is fine in a description; never
  imply the Jellyfin project made the app.
