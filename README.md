ComfyUI Batch Runner
A simple, standalone desktop tool for batch processing folders of images through any ComfyUI workflow — one image at a time, automatically, without touching the ComfyUI interface.

The Problem This Solves
ComfyUI is powerful but batch processing a folder of images is surprisingly painful. Queue Instant mode loops the same image forever. Directory loader nodes don't auto-increment. Tutorials show features that no longer exist in the current UI.
This tool sidesteps all of that. You load your workflow, point it at a folder, hit Start, and walk away.

Features

Universal — works with any workflow_api.json exported from ComfyUI
Auto-detects image loader nodes — supports both directory-based loaders (Inspire pack) and single LoadImage nodes
Two modes, automatically selected:

Index mode — increments start_index each run (Inspire pack LoadImagesFromDir etc.)
File mode — copies each image into ComfyUI's input folder and sets the filename (standard LoadImage node)


Resume support — Skip First N field lets you pick up where you left off after an interruption
Live log — colour-coded output shows exactly what's happening
Progress counter and progress bar
Open Output button — jumps straight to ComfyUI's output folder
Connection status — shows whether ComfyUI is online before you start
Delay between images — optional pause for VRAM to settle between jobs
No installation required for the .exe version


Requirements

ComfyUI running locally (default: http://127.0.0.1:8188)
Your workflow exported in API format (workflow_api.json)
For index mode workflows: ComfyUI-Inspire-Pack
For background removal workflows: ComfyUI-RMBG or similar


Download & Run
Option A — Standalone exe (Windows, no Python needed)

Download ComfyUI Batch Runner.exe from the Releases page
Double-click to run
Windows Defender may scan it on first launch — this is normal for unsigned executables, give it a moment

Option B — Run from Python source
pip install requests Pillow
python comfyui_batch_runner.py

How to Use
Step 1 — Export your workflow from ComfyUI
In ComfyUI, get your workflow working for a single image manually. Then:

Click the ⋮ menu → Save (API format)
This saves a workflow_api.json file


⚠️ Must be API format, not the regular save format. They look similar but are different.

Step 2 — Load the workflow
Click Browse… next to Workflow JSON and select your exported file.
The app will:

Show a summary of what the workflow does
Detect the image loader node(s) and display them as checkboxes
Auto-fill the input folder if it finds a directory path in the workflow

Step 3 — Set your input folder
Point it at the folder containing the images you want to process. The image count will appear immediately.
Step 4 — Hit Start
The app submits one image at a time, waits for ComfyUI to finish, then moves to the next. Progress is shown live in the log and counter.
Resuming after interruption
If the batch stops partway through, set Skip First N to however many images already completed, then hit Start again.

Tested Workflows
Workflow typeNodeModeUltimate SD Upscale (Inspire pack)LoadImagesFromDir //InspireIndexBackground removal (RMBG/BEN2)LoadImageFile
Other workflows using these node types should work automatically. If your workflow uses a different image loader node, open an issue and we'll add support.

Troubleshooting
"No index driver nodes detected"
The app couldn't find a recognised image loader node. Check that your workflow uses a supported node type, or open an issue with your workflow_api.json.
ComfyUI shows OFFLINE
Make sure ComfyUI is running before hitting Start. The URL field defaults to http://127.0.0.1:8188 — change it if your setup is different.
File mode: images not loading
The app copies images to ComfyUI\input\ before each run. Make sure the app can find that folder — it checks the standard portable install path. If your ComfyUI is installed elsewhere, this path may need updating (raise an issue).
Antivirus scanning on first launch
Normal behaviour for unsigned PyInstaller executables. The exe contains no network code beyond talking to your local ComfyUI instance.

Building from Source
pip install requests Pillow pyinstaller
python -m PyInstaller --onefile --windowed --name "ComfyUI Batch Runner" --hidden-import=PIL --hidden-import=PIL.Image --hidden-import=requests comfyui_batch_runner.py
The exe will appear in the dist\ folder.

Credits
Built with the help of Claude (Anthropic) as a practical tool born out of frustration with ComfyUI's batch processing limitations.
Shared freely with the ComfyUI community — if it saves you time, pass it on.

Contributing
Issues and pull requests welcome. Common things that would make this better:

Support for additional image loader node types
Drag and drop for folder input
Workflow preset saving
Output folder configuration


License
MIT — do whatever you want with it.
