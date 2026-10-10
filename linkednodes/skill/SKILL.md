---
name: linkednodes
description: Turn a folder of code, notes, saved links (bookmarks, likes, read-later exports) and images into a knowledge graph, then answer questions from it. Use when the user runs /linkednodes, asks to map or graph a folder, or asks about a folder that already contains linkednodes-out/graph.json.
---

# LinkedNodes

LinkedNodes builds a knowledge graph of a folder. A Python tool does everything
that needs no judgement: it finds the files, parses code, reads bookmark and
likes exports, follows the links between notes, clusters the graph and writes the
outputs. Your part is the reading: saying what each note, image and saved link is
about, so that things from different sources meet at shared concepts.

Run every command from the folder being graphed, or pass that folder as the path.

## 1. Check the tool

```
python -m linkednodes --version
```

If this fails, tell the user to install it from their copy of the source
(`pip install -e <path to the LinkedNodes folder>`) and stop. It is not published
on PyPI, so do not install a package of that name from there.

## 2. Scan

```
python -m linkednodes scan "<folder>"
```

The scan prints what it found and how many chunks of semantic work are queued. If
it reports more than 40 chunks, tell the user the count and ask before reading
them, because each chunk is a batch of files for you to read.

If nothing is queued, go straight to step 4.

## 3. Read the queue

Open `linkednodes-out/.work/todo.json`. Each chunk has an `id`, a `type` and its
content. Read `vocabulary_file` first: it lists concept labels already in the
graph. Reuse a label from it, spelled the same way, whenever it fits. Two labels
for one idea split the graph; one shared label is what connects a bookmark to a
note.

For every chunk, write one file named `<chunk id>.json` in `results_dir`.

With more than six chunks, hand them to parallel subagents, giving each its
chunks, this section of the instructions and the vocabulary. Otherwise do them
yourself, in order.

### Chunks of type `docs` and `images`

Read each file in `files` (paths are relative to the scanned folder) and write:

```json
{
  "files": {
    "notes/attention.md": {
      "summary": "One sentence saying what this file is.",
      "nodes": [
        {"label": "Self-attention", "kind": "concept", "note": "Optional short definition."}
      ],
      "edges": [
        {"source": "@file", "target": "Self-attention", "relation": "explains", "confidence": "EXTRACTED"},
        {"source": "Self-attention", "target": "Transformer", "relation": "part_of", "confidence": "INFERRED", "score": 0.8}
      ]
    }
  }
}
```

- Every file in the chunk needs an entry, even if it is `{"summary": "...", "nodes": [], "edges": []}`.
- `source` and `target` are one of: `"@file"` (the file being read), a concept
  label, or the path of another file in the folder, such as `"src/auth.py"`.
- `kind` is `concept`, `person`, `org`, `tool`, `paper`, `place` or `event`.
- `relation` is a short verb phrase in snake_case: `explains`, `uses`, `part_of`,
  `depends_on`, `contradicts`, `cites`, `example_of`, `authored_by`.
- `confidence` says where the edge comes from:
  - `EXTRACTED`: the file states it.
  - `INFERRED`: you concluded it. Add a `score` from 0 to 1.
  - `AMBIGUOUS`: plausible but unsure. These are listed in the report for review.
- Aim for the 3 to 12 ideas a file is really about. Skip passing mentions.
- Connect concepts to each other, not only to the file. Those edges are the graph.
- For an image, describe what it shows: the components of a diagram and how they
  relate, the subject of a screenshot, the content of a whiteboard.

### Chunks of type `saved`

Read `items_file`: a list of saved links with `id`, `title`, `url`, `folder`,
`tags` and `note`. Work from those fields only; do not fetch the URLs. Write:

```json
{
  "saved": {
    "saved:3f9a1c2b7d10": ["Rust", "Async runtimes"],
    "saved:77c0e41a9b02": ["Sourdough baking"]
  },
  "links": [
    {"source": "Async runtimes", "target": "Rust", "relation": "part_of", "confidence": "INFERRED"}
  ]
}
```

- Give every item 1 to 3 topics. Use `[]` only when the title and URL say nothing.
- Prefer a specific topic to a broad one: `Async runtimes`, not `Programming`.
- Use `links` to tie specific topics to broader ones, so that a hierarchy forms.
- Topics and the concepts from notes share one namespace. Reuse labels across both.

## 4. Build

```
python -m linkednodes build "<folder>"
```

Fix anything it warns about: a result that is not valid JSON, a path that was not
in the queue, files still missing an extraction. Then build again.

## 5. Name the communities

The build lists the communities with an automatic label and an anchor node id.
Read `linkednodes-out/GRAPH_REPORT.md`, then write `linkednodes-out/labels.json`,
mapping each anchor id to a name of two to five words that says what the cluster
is about:

```json
{"concept:rust": "Rust and systems programming", "code:app/service.py": "Order service"}
```

Build once more so the names appear in the report and the viewer.

## 6. Report back

Tell the user, briefly:

- the size of the graph and what it is made of;
- the most connected nodes and the surprising connections from the report, with
  one line each on why they matter;
- what they keep saving, if the folder had saved content;
- two or three questions from the report that would be worth asking next.

Point them to `linkednodes-out/graph.html`, which opens in any browser.

## Answering questions from an existing graph

When `linkednodes-out/graph.json` exists, answer from the graph before opening
source files:

```
python -m linkednodes query "<words>" --root "<folder>"
python -m linkednodes node "<name>" --root "<folder>"
python -m linkednodes path "<from>" "<to>" --root "<folder>"
```

`query` shows the best matches with their neighbours, `node` shows everything
about one node and `path` shows how two nodes are connected. Read
`GRAPH_REPORT.md` first for orientation. Say when an answer rests on an inferred
or ambiguous edge, and open the source file when the user needs the detail.

To refresh a graph after files change, repeat steps 2 to 4. Only new and changed
content is queued.

## Git

In a git repository, `node` and `query` add a `git:` line to each node: when its
file last changed, who changed it and in how many commits. To see what the user is
working on and what it may break, run:

```
python -m linkednodes changed --root "<folder>"
python -m linkednodes changed --base main --root "<folder>"
```

It lists the files that differ from the commit (uncommitted work included), the
functions and methods whose lines changed, and the nodes that use them. Use it
before answering "what does this change affect" or reviewing work in progress.
Which functions changed is worked out from line numbers recorded at the last
build, so build again first if the code moved a lot since then. The command only
reads the repository.

The graph also has `changes_with` edges between files that git shows being
committed together; a pair with no other link between them is a coupling the code
does not show, and the report lists those under "Change together". With several
authors, each is a `person` node with `authored` edges to their files.

`python -m linkednodes refresh "<folder>"` runs scan and build in one step; it is
what the optional git hook calls. Do not install the hook yourself: tell the user
about `python -m linkednodes hook install` and let them decide.

## Notes written by the user

The user can attach a Markdown note to any node and correct a node's name, kind or
description. Notes are files in `linkednodes-out/notes/`; corrections are in
`linkednodes-out/nodes.json`. Both are applied on every build, and `query` and
`node` print a node's note under `notes:`. Treat a note as the user's own word on
that node: prefer it to what was inferred, and mention it when it answers the question.

To let the user edit, tell them to run one of these in a terminal of their own,
since both keep running until closed. The first opens a desktop window and needs
pywebview; the second uses the browser. Either one follows the working tree as it
changes, so the user sees your edits as diffs while you work, and can stage,
commit, switch branch and push from its Git section:

```
python -m linkednodes app "<folder>"
python -m linkednodes serve "<folder>"
```

If the user asks you to write or change a note, edit the file in
`linkednodes-out/notes/` and build again. A new note needs
`<!-- linkednodes: <node id> -->` as its first line.
