"""LomboShrink end to end with a real ffmpeg and ffprobe, on throwaway files inside a container.

Real: scanning, probing, encoding (x265, the processor encoder), every check, the session lock and the server-side
swap. Stood in: ssh and scp are two small local scripts (the "server" is the same container), and no graphics card
is used. Run from the repository:

    docker build -t lf-shrink:test .
    docker run --rm -v "$PWD/test/real:/t:ro" --entrypoint python lf-shrink:test /t/run.py
"""

# ruff: noqa: S103, S108, S603  (throwaway scripts and files inside a test container)

import hashlib
import json
import os
import shutil
import subprocess
import sys

WORK = "/tmp/lf-shrink"
DISK1, DISK2, OUT, STAGE = (f"{WORK}/{n}" for n in ("disk1", "disk2", "pc-out", "stage"))
failures: list[str] = []


def check(name: str, ok: object, detail: object = "") -> None:
    print(("[ ok ] " if ok else "[FAIL] ") + name + ("" if ok or detail == "" else f"  -> {str(detail)[-700:]}"))
    if not ok:
        failures.append(name)


def sh(*cmd: str, env: dict[str, str] | None = None) -> tuple[int, str]:
    r = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, **(env or {})}, cwd=WORK)
    return r.returncode, r.stdout + r.stderr


def make(path: str, *codec: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    srt = f"{WORK}/one.srt"
    cmd = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=4"]
    cmd += ["-f", "lavfi", "-i", "sine=duration=4"]
    if path.endswith(".mkv"):
        cmd += [
            "-i",
            srt,
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "2:s",
            "-c:s",
            "srt",
            "-metadata:s:a:0",
            "language=fre",
        ]
    subprocess.run([*cmd, *codec, "-pix_fmt", "yuv420p", "-c:a", "aac", path], check=True)
    os.utime(path, (1_700_000_000, 1_700_000_000))


def streams(path: str) -> list[tuple[str, str, str]]:
    out = sh("ffprobe", "-v", "error", "-show_streams", "-of", "json", path)[1]
    return [
        (s["codec_type"], s["codec_name"], s.get("tags", {}).get("language", "")) for s in json.loads(out)["streams"]
    ]


def digest(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


shutil.rmtree(WORK, ignore_errors=True)
for d in (DISK1, DISK2, OUT, STAGE):
    os.makedirs(d)
with open(f"{WORK}/one.srt", "w") as f:
    f.write("1\n00:00:00,500 --> 00:00:02,000\nhello\n")
big = ("-c:v", "libx264", "-qp", "0", "-preset", "ultrafast")
film = "movies/Big Film (2016) é/Big Film (2016) é.mkv"  # spaces and an accent
clip = "movies/Clip (2012)/Clip's (2012).mp4"  # an apostrophe, another container, no language tag
make(f"{DISK1}/{film}", *big)
make(f"{DISK1}/{clip}", *big)
make(
    f"{DISK1}/movies/Done (2020)/Done.mkv", "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=none"
)
make(f"{DISK1}/movies/Seeding (2019)/Seeding.mkv", *big)
os.link(f"{DISK1}/movies/Seeding (2019)/Seeding.mkv", f"{WORK}/torrent-copy")
make(f"{DISK1}/movies/Leaving (2018)/Leaving.mkv", *big)
with open(f"{WORK}/skip.txt", "w") as f:
    f.write("# about to be deleted\nmovies/Leaving (2018)\n")
before = {rel: digest(f"{DISK1}/{rel}") for rel in (film, clip)}
film_streams = streams(f"{DISK1}/{film}")

server = {
    "LOMBO_SHRINK_DRY_RUN": "false",
    "LOMBO_SHRINK_STAGE_DIR": STAGE,
    "LOMBO_SHRINK_BRANCHES": f"{DISK1}:{DISK2}",
    "LOMBO_SHRINK_MARGIN_GB": "0",
    "LOMBO_SHRINK_SESSION_FILE": f"{WORK}/session.json",
}
with open(f"{WORK}/server.env", "w") as f:
    f.write("".join(f"export {k}='{v}'\n" for k, v in server.items()))
with open(f"{WORK}/ssh", "w") as f:  # ssh -o BatchMode=yes TARGET COMMAND
    f.write(
        f'#!/bin/sh\ncd {WORK}\nunset $(env | grep -o "^LOMBO_SHRINK_[A-Z_]*")\n. {WORK}/server.env\nexec sh -c "$4"\n'
    )
with open(f"{WORK}/scp", "w") as f:  # scp -o BatchMode=yes LOCAL TARGET:PATH
    f.write(f'#!/bin/sh\ncd {WORK}\nexec cp "$3" "${{4#*:}}"\n')
for name in ("ssh", "scp"):
    os.chmod(f"{WORK}/{name}", 0o755)

pc = {
    "LOMBO_SHRINK_LIBRARY_DIR": DISK1,
    "LOMBO_SHRINK_OUT_DIR": OUT,
    "LOMBO_SHRINK_ENCODER": "x265",
    "LOMBO_SHRINK_SPARE_GB": "0",
    "LOMBO_SHRINK_SKIP_FILE": f"{WORK}/skip.txt",
    "LOMBO_SHRINK_SHIP_TARGET": "me@server",
    "LOMBO_SHRINK_SSH_COMMAND": f"{WORK}/ssh",
    "LOMBO_SHRINK_SCP_COMMAND": f"{WORK}/scp",
    "LOMBO_SHRINK_REMOTE_STAGE_DIR": STAGE,
}


def shrink(*args: str, live: bool = True, **more: str) -> tuple[int, str]:
    return sh("lombo-shrink", *args, env={**pc, "LOMBO_SHRINK_DRY_RUN": "false" if live else "true", **more})


def untouched() -> bool:
    return all(os.path.isfile(f"{DISK1}/{r}") and digest(f"{DISK1}/{r}") == h for r, h in before.items())


code, out = shrink("candidates", "--out", f"{WORK}/list.json")
with open(f"{WORK}/list.json", encoding="utf-8") as f:
    found = json.load(f)
found = found["items"] if isinstance(found, dict) else found
check("candidates: only the two worth converting", sorted(i["path"] for i in found) == sorted([film, clip]), found)
shrink("run", live=False)
check("a dry run writes nothing", untouched() and not os.listdir(OUT))

code, out = shrink("run")
check("no session on the server: converted, but the library is not touched", untouched(), out)
waiting = [n for _, _, names in os.walk(OUT) for n in names if n.endswith(".hevc.mkv")]
check("the converted files wait on the PC", len(waiting) == 2, out)

code, out = sh("sh", "-c", f". {WORK}/server.env; lombo-shrink session open 1")
check("session open", code == 0, out)
code, out = shrink("run")
print("       " + out.strip().replace("\n", "\n       ")[-900:])
new_film, new_clip = f"{DISK1}/{film}", f"{DISK1}/{clip[:-4]}.mkv"
check("the next run ships the waiting files and swaps them", code == 0 and not untouched(), out)
check("the film is now H.265 under the same name", streams(new_film)[0][1] == "hevc", streams(new_film))
check("audio, its language and the subtitle are kept", streams(new_film)[1:] == film_streams[1:], streams(new_film))
check(
    "the new file plays through with no error", sh("ffmpeg", "-v", "error", "-i", new_film, "-f", "null", "-")[1] == ""
)
check(
    "it is smaller and kept the old date",
    os.path.getsize(new_film) < 0.8 * 10**7 and os.path.getmtime(new_film) == 1_700_000_000,
)
check(
    "the mp4 became an mkv and the old name is gone", os.path.isfile(new_clip) and not os.path.exists(f"{DISK1}/{clip}")
)
check(
    "the seeding file and the listed one were left alone",
    all(
        streams(f"{DISK1}/{r}")[0][1] == "h264"
        for r in ("movies/Seeding (2019)/Seeding.mkv", "movies/Leaving (2018)/Leaving.mkv")
    ),
)
left = [
    os.path.join(d, n)
    for root in (OUT, STAGE, DISK1, DISK2)
    for d, _, names in os.walk(root)
    for n in names
    if n.endswith((".partial.mkv", ".part", ".hevc.mkv")) or "lombo-shrink-part" in n
]
check("no converted or half-written files left anywhere", not left, left)
code, out = shrink("run")
check("running again finds nothing to do", code == 0 and "hevc" not in "".join(os.listdir(OUT)), out)

print(f"\n{len(failures)} failed" if failures else "\nall passed")
sys.exit(1 if failures else 0)
