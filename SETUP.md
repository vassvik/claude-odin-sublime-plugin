# Setup

## Requirements

- Sublime Text 4 (Build 4000+)
- An Odin syntax highlighting package (e.g. `Odin` from Package Control)

## Installation

### 1. Copy the plugin into Sublime's Packages directory

Find your Sublime Packages folder:
- **Windows**: `%APPDATA%\Sublime Text\Packages\`
- **macOS**: `~/Library/Application Support/Sublime Text/Packages/`
- **Linux**: `~/.config/sublime-text/Packages/`

Copy or symlink the `odin-sublime-plugin` folder there:

**Windows (copy):**
```
xcopy /E /I "path\to\odin-sublime-plugin" "%APPDATA%\Sublime Text\Packages\odin-sublime-plugin"
```

**Windows (symlink, requires admin or Developer Mode):**
```
mklink /D "%APPDATA%\Sublime Text\Packages\odin-sublime-plugin" "path\to\odin-sublime-plugin"
```

**macOS/Linux:**
```
ln -s /path/to/odin-sublime-plugin ~/Library/Application\ Support/Sublime\ Text/Packages/odin-sublime-plugin
```

### 2. Restart Sublime Text

The plugin requires a full restart on first install (because of `.python-version`).

### 3. Open a project containing Odin files

The plugin auto-indexes all `.odin` files in your project folders on startup. It also auto-detects the Odin root by searching upward for a directory containing `core/` and `vendor/`.

Check the status bar — it should show `Odin: Indexed N symbols` when done.

## Files

| File | Purpose |
|------|---------|
| `odin_plugin.py` | The plugin — parser, indexer, completions, navigation |
| `.python-version` | Tells ST4 to use Python 3.8 (required for f-strings) |
| `Default.sublime-keymap` | F12 = Go to Definition, Shift+F12 = Find References |
| `Default.sublime-commands` | Command palette entries |
| `odin-sublime-plugin.sublime-settings` | Configuration (Odin root override, extra dirs) |

## Usage

| Action | Keybinding | Notes |
|--------|-----------|-------|
| Autocomplete | Type normally | Shows symbols from current package + import aliases |
| Package completion | `jui.` | Shows symbols from that package |
| Struct field completion | `ctx.style.` | Resolves type chain, shows fields |
| Enum variant completion | `Color_Type.` | Shows enum variants |
| Implicit enum selector | `.` in function args | Resolves expected param type, shows variants |
| Go to definition | F12 | Jumps to declaration. Quick panel if ambiguous. |
| Find references | Shift+F12 | Grep-based, results in output panel |
| Hover info | Mouse hover | Shows signature, fields/variants, source location |
| Reindex | Ctrl+Shift+P → "Odin: Reindex Project" | Manual full reindex |
| Jump back | Alt+- | Sublime built-in, works after F12 |

## Configuration

Edit `odin-sublime-plugin.sublime-settings` to override defaults:

```json
{
    // Override Odin root path (auto-detected by default)
    "odin_root": "C:/path/to/odin",

    // Extra directories to index
    "extra_index_dirs": []
}
```

## Updating

If you used `xcopy` (not symlink), re-copy after changes:

**Windows:**
```
xcopy /E /Y "path\to\odin-sublime-plugin" "%APPDATA%\Sublime Text\Packages\odin-sublime-plugin"
```
