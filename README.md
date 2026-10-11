# LinkedNodes

LinkedNodes turns a folder into a knowledge graph. It reads four kinds of content
and connects them:

- **Code**: files, classes, functions, imports, calls and inheritance.
- **Notes**: Markdown, text and PDF files, with the links between them.
- **Saved content**: bookmark, likes and read-later exports.
- **Images**: diagrams, screenshots and whiteboards.

The point is the connections across kinds: a bookmark, a note and a function that
are all about the same idea end up attached to the same concept node.

It runs as a Claude Code skill. A Python tool does everything mechanical, and
Claude reads the notes, images and saved links to say what they are about.

## Install

```
pip install -e .
python -m linkednodes install
```

The second command copies the skill to `~/.claude/skills/linkednodes/`. Add
`--project` to install it into the current project's `.claude` folder instead.

## Use

In Claude Code, from the folder you want to graph:

```
/linkednodes .
```

Claude scans the folder, reads what needs reading, builds the graph and tells you
what it found. The results land in `linkednodes-out/`:

| File | What it is |
| --- | --- |
| `graph.html` | Interactive graph. Opens in any browser, works offline. |
| `GRAPH_REPORT.md` | Most connected nodes, surprising connections, communities, what you keep saving. |
| `graph.json` | The full graph, for queries and for other tools. |

Afterwards you can ask questions about the folder and Claude answers from the
graph. The same queries work from a terminal:

```
python -m linkednodes query "pricing"
python -m linkednodes node "total_price"
python -m linkednodes path "Tokio tutorial" "Rust"
```

Run `/linkednodes .` again after changes. Only new and changed files are read.

## The viewer

`graph.html` has a search box in the top bar (press `/`), filters by node kind and
by number of connections, colouring by community or by kind, and sliders for labels,
node size, link thickness and the layout forces: centre, repel, link strength and
link distance. Selecting a node shows its connections, and can narrow the graph to
its neighbours. The interface is dark and dense, and is in Spanish when the browser
is, in English otherwise.

The side panels slide over the graph instead of resizing it, so opening or closing
one never redraws or clears the canvas; the camera moves so that what you were
looking at stays in view.

## Desktop window

To use the graph as a program of its own, without a browser tab or a terminal:

```
pip install -e ".[app]"
python -m linkednodes app .
```

It opens the same viewer, with editing, in a native window. The window is drawn by
the web view the system already has (WebView2 on Windows), through pywebview, so no
browser is bundled. The File menu opens another folder, building its graph first if
it has none, and rebuilds the current one; the Language menu switches between
Spanish and English. On Windows, `linkednodes-out/open-graph.ps1` opens this window
when pywebview is installed and falls back to the browser when it is not.

While a folder is read for the first time, or built again from the File menu, the
window shows what is being found as it is found: the files, the nodes and relations
counted so far, the repository, its branch and its commits, the languages, and each
step as it starts and ends, beside a small graph that grows a cluster a step. The
animation is [Anime.js](https://animejs.com), which travels inside the page, so it
needs no network. With the system's "reduce motion" setting on, the page only counts.

The window is light on disk, not on memory: the web view is the same engine as the
Edge browser and uses about as much as one of its tabs.

### GitHub

The window's start page (File, then GitHub repositories) has a "Sign in with GitHub"
button. It shows a code and opens github.com, where you type the code and approve.
From then on the start page lists the repositories your account owns, collaborates
on or reaches through an organization, with a box to filter them. Click one to open
it: a repository that is already on this computer, among the recent folders or
cloned from here before, is opened where it is; any other is cloned first, into
`LinkedNodes/owner/name` under your home folder. Another place can be named as
`workspace` in the window's `settings.json`.

- **The token** is kept in Windows Credential Manager, and on other systems in a file
  that only you can read. It is never in the settings and never reaches a page. For a
  clone it is handed to git for that one command; what the clone fetches and pushes
  later goes with the credentials git itself has.
- **The permission** asked for is `repo` and `read:org`: read and write on every
  repository you can reach. That is what listing private repositories takes, and
  GitHub has nothing narrower for this kind of sign-in. An organization may hide its
  repositories until one of its owners approves LinkedNodes.
- **Signing out** forgets the token on this computer. To make GitHub forget it too,
  revoke LinkedNodes under Settings, Applications, Authorized OAuth Apps.
- **Another registration** of the program with GitHub can be used by putting its
  client id in the `LINKEDNODES_GITHUB_CLIENT` environment variable. It needs device
  flow enabled, and no secret.

### As a program, without Python

```
pip install -e ".[app,build]"
python packaging/build.py --install
```

This builds `dist/LinkedNodes/LinkedNodes.exe` with PyInstaller, a folder of about
35 MB that runs on a Windows machine with no Python. `--install` copies it to
`%LOCALAPPDATA%\Programs\LinkedNodes` and adds Desktop and Start Menu shortcuts;
`--uninstall` removes them. Started on its own, the program opens the last folder
used, or a start page to pick one. Opening a folder that has no graph builds one.
Installing over a copy that is running stops with a message and leaves that copy
whole: close the window and run it again.

What the program cannot do by itself is the reading: saying what notes, images and
saved links are about is still Claude's part, through the skill, and the skill
calls `python -m linkednodes`. A folder opened only from the program gets the
structural graph: code, links between notes, saved items and git.

The same executable runs any command without a window, for scripts on a machine
with no Python: `LinkedNodes.exe refresh <folder>`. It prints nothing.

It is not signed, so Windows may warn the first time it runs on another machine.

## Git

When the folder is a git repository, the graph also carries what git knows. Nothing
in this list writes to the repository, apart from the optional hook at its end. What
does write, when you ask it to, is the next section.

- **What changed and what uses it.** The Git section of the viewer lists the files
  that differ from the last commit, uncommitted work included. Switch on the
  highlight and the changed nodes get an orange ring, the nodes that use them a
  dashed one, and the rest fades. Changes are narrowed down to the functions and
  methods whose lines were touched. With the server running the list follows the
  folder as it changes, and you can compare with any commit, branch or tag;
  `graph.html` on its own shows the state at build time.
- **History per node.** Each node's panel shows when its file last changed, who
  changed it and in how many commits. "Colour by" gains two options, activity and
  last change, which show history as brightness.
- **Files that change together.** Two files that share at least two commits, and at
  least half the commits of the quieter one, get a `changes_with` edge. Commits that
  touch more than 25 files are not counted as pairings. The report lists the pairs
  and says which have no other link between them: coupling the code does not show.
- **In the editor.** The Code tab shows a letter for the file's state (M, U, A), a
  bar beside every line that differs from the last commit, and a button that swaps
  the editor for the diff. The diff is what is saved on disk; unsaved edits are not
  in it.
- **Authors.** When at least two people each made a tenth of the commits, each
  becomes a node linked to the files they committed to. One person using two names
  with the same email counts once; with two emails, git cannot tell and neither can
  this.
- **From the terminal**, and for Claude:

  ```
  python -m linkednodes changed
  python -m linkednodes changed --base main
  ```
- **Automatic rebuild.** `python -m linkednodes hook install` adds `post-commit` and
  `post-merge` hooks that run `linkednodes refresh` in the background, so the graph
  follows the code. New notes and images still wait for Claude to read them. The
  hooks live in `.git/hooks`, are not committed, and a hook you already have is never
  overwritten. `python -m linkednodes hook remove` takes them out. This is the only
  command that writes inside `.git`.

History is per file, not per function: every function in a file shares its file's
dates and counts. Which functions changed is worked out from the line numbers
recorded at the last build, so it drifts if code has moved a lot since then.

### Working with git from the viewer

With the server or the desktop window, the Git section is also where the work gets
committed:

- **Live.** The section follows the working tree: save a file in any editor, or let
  an agent change it, and within a second or two the list, the highlight in the
  graph and the diff on show are up to date. A file open in the Code tab is read
  again from disk, unless it has edits of yours that this would lose.
- **Diffs.** Click a changed file to see its diff beside the graph, staged or not,
  and a commit in the list of the latest ones to see what it changed.
- **Stage and commit.** `+` and `−` move a file in and out of the commit, or all of
  them at once. Write the message and press Commit, or Ctrl+Enter.
- **Branches.** Pick a branch to switch to it, including one that only the remote
  has, or name a new one. After a switch the graph is rebuilt, since the files
  changed under it.
- **Remote.** Fetch, pull and push, with how far ahead and behind the branch is. The
  first push of a branch publishes it. Commits not yet on the remote are marked.

It runs the git you have, with your name, your hooks and your credentials, and shows
what git answers. Three things are left to a terminal on purpose: a pull only
fast-forwards, so merges and conflicts are not handled here; nothing discards
changes; and a push that needs a password typed fails instead of waiting.

## Editing nodes and attaching notes

A page opened from disk cannot write files, so editing runs through a local server:

```
python -m linkednodes serve .
```

On Windows, every build also writes `linkednodes-out/open-graph.ps1`, which does the
same for that folder: right click it and choose "Run with PowerShell". Add `-Browser`
to use the browser even when the desktop window is available.

It opens the same viewer with three more tabs on every node:

- **Note**: documentation for the node, written in Markdown. Each note is a file in
  `linkednodes-out/notes/`, so it can be committed with the project. Nodes with a
  note are ringed in the graph, and Claude reads the notes when it queries the graph.
  Saving one updates that note in the graph and nothing else, so it is quick however
  large the graph is.
- **Properties**: correct the node's name, kind or description. Corrections are kept
  in `linkednodes-out/nodes.json` and applied on every build.
- **Code**: the file the node comes from, opened at the node's line, editable and
  saved to disk. The editor follows Visual Studio Code's dark theme: syntax colours
  for the languages the scanner reads, line numbers, breadcrumbs, a status bar with
  the caret position, automatic indentation, Tab and Shift+Tab to indent a
  selection, and Ctrl+S to save. It has no completion or error checking. Rebuild the
  graph from the same tab to see the effect of a change.

The server listens on 127.0.0.1 only and refuses requests that do not carry the
token it hands to its own page. It edits only the code and text files that the scan
found in the folder, and refuses to save over a file that changed on disk after it
was opened. Its git requests take paths inside the folder and valid branch names,
and nothing else.

## How it works

1. **Scan** (`linkednodes scan`) finds the files, extracts everything that needs no
   model, and writes a queue of chunks to `linkednodes-out/.work/todo.json`.
2. **Read**: Claude works through the queue and writes one JSON result per chunk:
   the concepts in each note and image, and one to three topics per saved link.
3. **Build** (`linkednodes build`) validates the results, stores them in a cache
   keyed by file hash, merges everything, clusters the graph and writes the outputs.

Every edge carries a confidence label:

- `EXTRACTED`: stated in the source, such as an import or a link in a note.
- `INFERRED`: a conclusion, such as the topic of a bookmark.
- `AMBIGUOUS`: plausible but unsure; the report lists these for review.

## Saved content formats

| Source | File |
| --- | --- |
| Chrome, Firefox, Edge, Safari bookmarks | exported `.html` |
| Firefox bookmark backup | `.json` |
| X/Twitter archive | `like.js`, `bookmark.js` |
| YouTube Takeout | playlist `.csv`, history `.json` |
| Pocket, Raindrop, Instapaper, Reddit | export `.csv` or `.html` |
| Anything else | `.json` or `.csv` with a URL field, a `.txt` list of URLs, Windows `.url` shortcuts |

Only the title, URL, folder, tags and note of each item are used. Pages are not
downloaded.

## Limits

- Python is parsed with its syntax tree, so its edges are exact. JavaScript,
  TypeScript, Go, Rust, Java, C#, Ruby and PHP are read with regexes: definitions
  and imports are reliable, calls are best guesses and marked `INFERRED`.
- In a git repository, files that git ignores are skipped, so build output and
  generated code stay out of the graph. An export you keep out of git is skipped
  too; graph it from a folder that is not a repository.
- A JSON or CSV file with three or more URL records is treated as an export. List
  files that are not in `.linkednodesignore` (one glob pattern per line).
- The browser view shows the 4,000 most connected nodes of larger graphs. From a
  thousand nodes on, its layout takes the nodes far from each one a group at a time
  rather than one by one, which is what keeps it quick; a graph that size appears at
  once and goes on settling on screen for a few seconds.
- Two different spellings of one topic become two nodes. The skill gives Claude
  the existing vocabulary to prevent this, but it can still happen.

## Tests

```
python -m unittest discover -s tests
```

Two of the tests draw a graph in the viewer and count what reaches the canvas. They
run in Edge or Chrome without showing a window, and are skipped when neither is
installed. `.github/workflows/tests.yml` runs everything on Windows for each push.

## License

MIT. See [LICENSE](LICENSE). The only runtime dependency, NetworkX, is installed
separately and is under the BSD 3-Clause license; nothing from it is copied here.
Anime.js, which animates the page shown while a folder is read, is copied into
`linkednodes/vendor/` with its own MIT license beside it.
