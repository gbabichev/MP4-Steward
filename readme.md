# MP4 Converter for SABnzbd

This folder contains the automatic Python version of MP4 Tool. It is intended to run after SABnzbd finishes downloading and unpacking a job.

The short version: SAB calls `SAB-smart.py` to choose remux or H.265 automatically, `SAB-encode.py` to always encode, or `SAB-remux.py` to only repackage compatible streams. All three entry points use `MP4_Steward.py` for processing and safety checks.

## Files in this folder

- `SAB-smart.py` is the recommended SAB entry point. It remuxes compact compatible files and encodes larger or incompatible files.
- `SAB-encode.py` is the SAB entry point for H.265 encoding.
- `SAB-remux.py` is the SAB entry point for remuxing compatible tracks without re-encoding.
- `MP4_Steward.py` contains the actual probing, FFmpeg conversion, validation, file handling, progress reporting, and logging.
- `MP4_Converter.log` is created automatically when the script runs. It is not part of the source code and should not be committed.

## What happens to a downloaded video

For each supported video in the completed SAB folder, the converter:

1. Inspects the video, audio, and subtitle tracks with FFprobe.
2. Selects one preferred main English audio track. If no track is explicitly English, undefined-language audio is used as a fallback because many releases are not labeled correctly. Commentary, audio-description, and dubbed tracks are avoided when a main track is available.
3. Skips the file before encoding if no suitable audio track exists.
4. In `SAB-smart.py`, measures the input size against its runtime. Files at or below 25 MB/min are remuxed when their selected tracks are MP4-compatible; larger or incompatible files use H.265. `SAB-encode.py` always uses H.265 with CRF 23 and the `fast` preset. Apple-compatible AAC, ALAC, AC-3, and E-AC-3 audio is copied unchanged. MP3 and other incompatible audio are converted to high-quality AAC at a channel-appropriate bitrate while preserving the original mono, stereo, 5.1, or 7.1 layout.
5. In `SAB-remux.py`, copies compatible video and audio streams without re-encoding; incompatible inputs fail safely and should be processed with the encode script instead.
6. Keeps one compatible embedded full English subtitle track and converts it to MP4 text subtitles. Embedded text subtitles always take priority. When no usable embedded text subtitle exists, a matching sibling SRT is added automatically. Selection prefers ordinary subtitles, then SDH, then forced-only subtitles; cue counts and accessibility markers resolve unclear labels. Undefined-language embedded subtitles are used only when no English track is identified.
   Long video encodes complete and validate their video/audio output before subtitles are introduced in a fast second remux. This prevents sparse subtitle streams from making some FFmpeg versions finish a long encode prematurely.
7. Preserves language and useful dispositions, standardizes subtitle labels, and removes source attribution/title metadata.
8. Writes to a temporary file and validates it with FFprobe.
9. Applies the cleaned final filename and moves the validated MP4 beside the original source.
10. Deletes the original only after the output has passed validation and the final move has been verified.

If anything fails, the original file is kept.

## SABnzbd setup

Keep `SAB-smart.py`, `SAB-encode.py`, `SAB-remux.py`, and `MP4_Steward.py` in the same scripts folder. Make all four scripts executable:

```bash
chmod +x SAB-smart.py SAB-encode.py SAB-remux.py MP4_Steward.py
```

In SABnzbd, select the script appropriate for each category:

- Use `SAB-smart.py` for the same automatic size/runtime decision as MP4 Tool. This is the recommended default.
- Use `SAB-encode.py` when the downloaded video should be converted to H.265 MP4.
- Use `SAB-remux.py` when compatible streams should only be repackaged as MP4 without re-encoding.

SAB passes the completed download folder as the first argument automatically.

Both SAB entry points use:

- The completed download folder as both the input and output location
- The validated, automatically renamed MP4 placed beside its source file

Example result:

```text
Completed Download/
└── Movie Name (2026).mp4
```

After a successful conversion and validation, the original `.mkv` is removed. The renamed MP4 remains directly in the completed download folder.

## Requirements

- Python 3.10 or newer
- FFmpeg
- FFprobe

On macOS with Homebrew:

```bash
brew install ffmpeg
```

Files are processed using Python's built-in case-insensitive alphabetical sorting. No additional sorting package is required.

## Running it manually

The full converter can also be run without SAB:

```bash
python3 MP4_Steward.py "/path/to/input" "/path/to/output" smart=true subfolders=true rename=true
```

The converter takes two folder paths followed by readable `key=value` options:

1. Input folder
2. Output folder
3. `smart=true`, `encode=true`, or `remux=true`
4. `subfolders=true` or `subfolders=false`
5. Optional `rename=true` or `rename=false` (defaults to `true`)
6. Optional `smart_target=<MB/min>` (defaults to `25`)

Examples:

```bash
# Automatically remux compact compatible files and encode everything else
python3 MP4_Steward.py "/downloads/finished" "/media/converted" smart=true subfolders=true rename=true

# Use a more generous Smart remux threshold
python3 MP4_Steward.py "/downloads/finished" "/media/converted" smart=true subfolders=true rename=true smart_target=30

# Encode to H.265 and create a folder for each result
python3 MP4_Steward.py "/downloads/finished" "/media/converted" encode=true subfolders=true rename=true

# Remux compatible streams without re-encoding the video or audio
python3 MP4_Steward.py "/downloads/finished" "/media/converted" remux=true subfolders=false rename=true

# Encode beside each source while preserving its existing filename
python3 MP4_Steward.py "/downloads/finished" "/downloads/finished" encode=true subfolders=false rename=false
```

Automatic naming converts a release-style movie filename to `Movie Title (Year).mp4`. When subfolders are enabled, the same cleaned name is used for the directory. Set `rename=false`, or set `MP4_AUTOMATIC_RENAME=False`, to preserve source names.

Using the same input and output folder is supported regardless of whether the resolved `.mp4` name changes. For an exact same-name replacement, the converter copies the validated result to a temporary sibling, verifies it on the destination volume, and atomically replaces the source. A different pre-existing destination file is never overwritten.

## Progress in SAB

SAB receives live progress through the script's standard output. During a normal SAB run, it reports approximately once per minute and once more when the file finishes.

Example:

```text
[i] Progress: Processing Movie.mkv | 42.0% | 9m elapsed | ETA 12m | 1.06x
```

These progress updates are deliberately not written to `MP4_Converter.log`, which prevents status reporting from creating enormous log files.

Interactive terminal runs update progress more frequently on a single line.

## Logs

Normal events, selected tracks, the FFmpeg command, failures, and the final summary are written to `MP4_Converter.log` beside the scripts.

By default:

- The active log is limited to roughly 5 MB before rotation.
- Three older logs are retained as `.1`, `.2`, and `.3`.
- Frequent progress messages are excluded from the file log.
- FFmpeg failures include only the final meaningful diagnostic lines.

The following environment variables can change the defaults:

| Variable | Purpose | Default |
| --- | --- | --- |
| `MP4_CONVERTER_LOG` | Choose a different log file | `MP4_Converter.log` beside the script |
| `MP4_LOG_MAX_BYTES` | Maximum active log size before rotation | 5 MB |
| `MP4_LOG_BACKUP_COUNT` | Number of rotated logs to retain | 3 |
| `MP4_PROGRESS_INTERVAL_SECONDS` | How often SAB receives progress | 60 seconds |
| `MP4_PROCESSING_DIR` | Folder used for temporary MP4 files | `/data/downloads/processing` when available, otherwise the system temporary folder |
| `MP4_AUTOMATIC_RENAME` | Enable movie filename and subfolder cleanup when `rename` is omitted | `True` |
| `MP4_SMART_TARGET_MB_PER_MINUTE` | Smart-mode remux threshold | `25` |

## Exit codes

SAB uses the script's exit code to decide whether post-processing succeeded:

- `0`: Every discovered file completed successfully.
- `1`: A file failed, was skipped, or processing was cancelled.
- `2`: The script was called incorrectly.

A skipped file intentionally returns a failure to SAB so that it does not silently disappear from attention.

## Safety behavior

- Existing destination files are never overwritten unless the destination is the source file being safely replaced.
- Same-name source replacement uses a verified sibling file and an atomic final rename.
- Partial temporary outputs are removed after success, failure, skip, or cancellation.
- Ctrl+C, SIGTERM, and SAB cancellation stop FFmpeg. FFmpeg gets a short chance to stop normally before it is force-stopped.
- The original is deleted only after output validation and a verified final move.
- A failure in one file does not delete that file's source.

## Remux mode

Remux mode copies compatible video and audio streams without re-encoding them. It rejects known incompatible MP4 combinations, including unsupported video codecs, DTS, floating-point PCM, and other unsupported audio formats.

For incompatible files, use `encode` mode instead.

## Smart mode

Smart mode uses the same size-per-runtime rule as MP4 Tool. It calculates the input's decimal megabytes per minute and selects remux when the result is at or below the configured target. Before remuxing, it checks video codec, audio codec, channel-layout safety, and the selected tracks. If remux is unsafe—or file size/runtime cannot be read—it automatically falls back to H.265 encoding.

The default target is 25 MB/min. Override it with `smart_target=30` during a manual run or set `MP4_SMART_TARGET_MB_PER_MINUTE=30` for SAB.

## Important audio note

The converter no longer forces every audio track to 5.1. It preserves the layout reported by the source, which prevents the converter from creating fake surround audio.

Encoding the video no longer means automatically re-encoding its selected audio. AAC, ALAC, AC-3, and E-AC-3 are copied without quality loss. MP3 and other formats use AAC at 128 kb/s for mono, 256 kb/s for stereo, 384 kb/s for 3–4 channels, 512 kb/s for 5–6 channels, and 768 kb/s for 7–8 channels.

It does not automatically repair a source that was already authored as fake 5.1. Use the Swift app's MP4 Validator and audio repair feature for those existing files.

## Troubleshooting checklist

If SAB reports that the script failed:

1. Open `MP4_Converter.log` and read the final summary and error.
2. Confirm `ffmpeg` and `ffprobe` are available to the SAB process, not only to your interactive shell.
3. Confirm the SAB user can write to the output and processing folders.
4. Check whether the destination MP4 already exists.
5. Check whether the source has English or undefined-language audio.
6. Confirm there is enough free space for the temporary encode.

You can reproduce either type of SAB run manually with:

```bash
# Automatic remux or H.265 encode
python3 SAB-smart.py "/path/to/completed/SAB/job"

# H.265 encode
python3 SAB-encode.py "/path/to/completed/SAB/job"

# Remux without re-encoding
python3 SAB-remux.py "/path/to/completed/SAB/job"
```

The script scans supported video files in the top level of that folder. It currently recognizes `.mkv`, `.mp4`, `.avi`, `.mov`, and `.m4v` files, regardless of extension capitalization.

## After updating these scripts

Before putting a new version back into SAB:

1. Keep all four Python files together.
2. Restore executable permissions if the files were copied through a system that removed them.
3. Test with a copied short video first.
4. Confirm SAB shows live progress.
5. Confirm the output plays correctly before using it on the full queue.
