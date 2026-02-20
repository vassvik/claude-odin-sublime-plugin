# odin-sublime-plugin

Sublime Text 4 plugin providing IDE-like features for Odin. ~650 lines of Python, no dependencies beyond ST4's plugin API.

## What it does

### Symbol Indexer
- Parses all `.odin` files in project folders on startup + on file save
- Extracts: procs (with full signatures, params, return types), structs (with fields, including `using` inheritance), enums (with all variants), unions, constants, type aliases, top-level variables
- Parses import statements and resolves them: `core:`, `base:`, `vendor:` collections + relative paths like `"../jui"`
- Auto-detects Odin root per project (searches upward for `core/` + `vendor/` dirs)
- Indexes imported stdlib/vendor packages based on actual usage (if you `import "core:math"`, it indexes `math`)
- Background threading, cached completions for responsiveness

### Autocomplete
- Rich completions with full function signatures shown as annotations: `draw_rect` → `(ctx: ^Context, rect: Rect, color: Color)`
- Package-aware: typing `jui.` shows only symbols from the jui package, `math.` shows math, etc.
- Struct field dot-completion with type chain resolution: `ctx.style.colors` resolves through pointer types and `using` fields
- Enum variant completion: `Color_Type.` shows `TEXT, BORDER, WINDOW_BG, ...`
- Implicit enum selectors: typing `.` inside a function call resolves the expected parameter type and shows enum variants
  - Works for: function arguments (tracks comma position), typed assignments (`x: Opt = .`), comparisons (`x == .`)
- Suppresses Sublime's built-in completions when showing contextual results

### Navigation
- **Go to definition** (F12) — jumps to the symbol declaration with correct line and column. Shows a quick panel with previews when multiple definitions exist (e.g. overloaded names across packages).
- **Find references** (Shift+F12) — whole-word grep across all `.odin` files, results shown in a navigable output panel
- **Hover popup** — mouse over any symbol to see its type signature, struct fields or enum variants, and source location
- **Reindex** command in palette for manual refresh

### Configuration
- `odin-sublime-plugin.sublime-settings` — Odin root override, extra index dirs
- `Default.sublime-keymap` — F12 / Shift+F12 scoped to `source.odin`
- `.sublime-commands` — command palette: Go to Definition, Find References, Reindex Project

## Status

**Working** — all core features functional. Tested on the EmberGen codebase (~59k+ symbols across project + stdlib).

### Known limitations / future work
- No type inference for `:=` local variables (would need return type resolution)
- No struct field completion for chained method-style calls
- No scope awareness (local vs global shadowing)
- Struct literal field type resolution for implicit selectors (`.` inside `Struct{ field = .}`) not yet implemented
- No semantic understanding of `when` blocks or build tags

## Architecture

Single Python file (`odin_plugin.py`) containing:
1. **Data classes** — `Symbol` (name, kind, signature, file, line, col, fields, variants, params, return_type, etc.) and `ImportInfo`
2. **Parser** — line-by-line state machine with multi-line collection for structs/enums/procs. Handles block comments, `@private` attributes, `using` fields, parameterized structs, proc groups, bit_sets.
3. **Index** — thread-safe symbol store keyed by name and package directory. Import resolution, type lookup, field chain resolution, enum-for-type resolution.
4. **Completions** — cached per-package, prefix-filtered. Dot-completion dispatches to package/struct-field/enum/implicit-enum paths.
5. **Navigation** — goto-def with encoded position, find-refs via file scan, hover via minihtml popup.

## Odin patterns handled

```odin
// Procs (single + multi-line, generic, calling conventions)
name :: proc(params) -> return_type { ... }
name :: #force_inline proc "contextless" (params) -> ret { ... }
name :: proc{overload1, overload2}  // proc group

// Structs (with fields, using, parameterized)
Rect :: struct { x, y, w, h: i32 }
Stack :: struct($T: typeid, $N: int) { idx: i32, items: [N]T }

// Enums (single-line and multi-line, with values)
Clip :: enum u32 { NONE, PART, ALL }

// Types, bit_sets, distinct
Id :: distinct u32
Options :: distinct bit_set[Opt; u32]  // tracks underlying enum

// Constants + #config
MAX_SIZE :: #config(KEY, 1024)

// Imports (collections + relative)
import "core:math"
import jui "../jui"
```
