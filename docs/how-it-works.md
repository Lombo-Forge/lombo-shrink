# How it works

## Contents

- [Why 10-bit and CQ 26](#why-10-bit-and-cq-26)
- [Skipped before encoding](#skipped-before-encoding)
- [Accept rules](#accept-rules)
- [Statuses and resume](#statuses-and-resume)
- [The swap](#the-swap)
- [Measured results](#measured-results)

## Why 10-bit and CQ 26

Measured on one RTX 3080 with a 30 second clip of 1080p camera footage (SSIM and PSNR against the source):
10-bit at CQ 26 was about 19% smaller than 8-bit at CQ 24 for the same quality (SSIM 0.987, PSNR 45 dB), and
about 4.8x real time. Slower presets and several encodes at once did not help. A processor-only encode
(`x265`, slow) was about 15% smaller again but about 17 times slower, which is why graphics cards are used.
These numbers are for NVIDIA; other encoders use the same idea with their own quality scale, unmeasured.

## Skipped before encoding

Nothing is encoded, and the reason is logged, when the source has no video stream or more than one real video
stream (cover art is not counted); is already HEVC (unless `ALLOW_HEVC`); is interlaced; is HDR (PQ or HLG) or
Dolby Vision; has a pixel format other than 4:2:0 8-bit or 10-bit; has a subtitle codec mkv cannot copy; or has
fewer than `MIN_BPP` (0.08) bits per pixel per frame, because a source that is already starved of bitrate
saves little and looks worse.

Kept in the output: video, audio and subtitles as they are (mov_text becomes srt), attachments, chapters and the
colour tags. The container is always mkv.

## Accept rules

The output is accepted only if all hold, else the original stays and every broken rule is in the log:

- at least `MIN_SAVING_PCT` (20%) smaller;
- ffprobe can read it;
- the counts of video, audio, subtitle and attachment streams and of chapters match;
- width, height, aspect ratio (sample aspect ratio when there is no display ratio) and frame rate match;
- the video packet count matches;
- per audio and subtitle track: codec, language and default/forced flags match (the encode copies "not default"
  explicitly, because ffmpeg alone would mark the first subtitle default and players would then show subtitles);
- the duration differs by 1.5 s or less;
- a full decode (`-xerror`) prints nothing and exits 0.

The encode is killed and logged as failed if the output file does not grow for `STALL_MINUTES`, or if it runs
longer than twice the source length plus ten minutes. Hardware decode is tried first (NVIDIA and Intel); if that
fails, the encode is retried once with processor decoding.

## Statuses and resume

| Status | Meaning | Retried? |
|---|---|---|
| `accepted` | converted, checked | no (but if shipping is on and the file was not swapped in, the next run ships it again without re-encoding) |
| `skipped` | a skip rule applied | no |
| `rejected` | a quality, size or integrity rule said no | no (a starved source would only get harsher quantisation) |
| `missing` | the source file was not found | no |
| `failed` | transient: ffmpeg crashed or stalled, I/O error | yes, next run |

`lombo-shrink run --redo` also redoes accepted, rejected and skipped files (limits and STOP still apply).

## The swap

See the list in `src/lombo_shrink/swap.py`. In short: session open, staged file verified by size and sha256,
original unchanged, staged video is HEVC (when ffprobe exists on the server), room on the original's disk,
copy next to the original, sha256 again, copy mode/owner/time, rename into place, remove the original (or move it
to `.lombo-shrink-holding/` inside the library folder with `KEEP_ORIGINALS`, so it is always on the same disk),
then clean the staging folder. The original is checked once more right before the rename; a change in the few
microseconds after that check is not caught.

On the PC, the output name of a non-mkv source keeps its extension (`A.mp4` becomes `A.mp4.hevc.mkv`), so an
`A.mkv` and an `A.mp4` in one folder never share an output file.

With mergerfs, set `BRANCHES` to the library folder on each disk (colon separated) so the new file lands on the
same disk as the original; writing through the pool path would let the pool's create policy choose a disk.

A repeat after a lost answer reports `skipped / already swapped` and counts as success, so the PC can clean up.

## Measured results

Whole films, original private-stack measurements (RTX 3080, preset p6, 10-bit):

| Source | CQ | Size vs source |
|---|---|---|
| Blu-ray rip, about 10 Mbps | 22 / 24 / 26 | 82% / 62% / 47% |
| Web download, about 5.5 Mbps | 26 | 73% |

Low-bitrate web sources save much less, and can grow at low CQ, which is what `MIN_BPP` and `MIN_SAVING_PCT`
guard against. Your savings will vary.
