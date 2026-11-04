# SortKeeper

Organize your Downloads folder in one click. SortKeeper sorts loose files into Images, Screenshots, Videos, Screen Recordings, Audio, PDFs, Documents, Spreadsheets, Presentations, Archives, Installers, Code, Fonts, Design and Other. It catches duplicate files and lets you undo the last run. Everything stays on your computer.

Website: https://onemandeveloper.com/sortkeeper/

## Download

| System | File |
| --- | --- |
| Windows 10+ | [SortKeeper-windows.exe](https://github.com/jaisudhakar/sortkeeper/releases/latest/download/SortKeeper-windows.exe) |
| macOS (Apple Silicon) | [SortKeeper-macos.zip](https://github.com/jaisudhakar/sortkeeper/releases/latest/download/SortKeeper-macos.zip) |
| Ubuntu 22.04+ / Linux x64 | [SortKeeper-linux-x64](https://github.com/jaisudhakar/sortkeeper/releases/latest/download/SortKeeper-linux-x64) |

**macOS:** the app isn't signed with an Apple developer certificate, so the first time you open it, right-click **SortKeeper**, choose **Open**, then click **Open** again.

**Linux:** make the file executable first: `chmod +x SortKeeper-linux-x64 && ./SortKeeper-linux-x64`

## Run from source

```
sudo apt install python3-tk    # Ubuntu, once
python3 sortkeeper.py
```

Command-line mode:

```
python3 sortkeeper.py --cli [FOLDER]           # preview only
python3 sortkeeper.py --cli --apply [FOLDER]   # move files
python3 sortkeeper.py --cli --undo [FOLDER]    # undo last run
```

## Releasing a new version

Change `APP_VERSION` in `sortkeeper.py` (for example to `1.0.1`) and push to `main`. GitHub Actions builds the Windows, macOS and Linux files and publishes them as release `v1.0.1`. The download links above always point to the latest release.
