# linga

A Myanmar (Burmese) text toolchain: syllable segmentation, POS tagging and
sentence analysis, behind a web app that takes a PDF/DOCX/TXT upload and hands
back checked, converted text.

> **This repository is not one tree. It is one service per branch, and `main`
> is the MAP — not the application.** If you came here to read or run code, the
> table below is the whole answer; the rest of this file explains the two traps
> that cost the most time.

## Start here

| Read | For |
| --- | --- |
| `README.md` (this file) | what the repo is, which branch has the code |
| `AGENTS.md` | the rules that bite — read before editing anything |
| `PROJECTSTRUCTURE.md` | the detailed map: per-branch contents, the command that actually runs there, what is broken |
| `USAGE.md` | every command's flags and usage, in one document |
| `./.bin/branch.sh map` | the same map, printed live from the remote |

## The branch table

Every branch is a **service**, checked out at the repo root, and **no two of
them share history** (`git merge-base origin/main origin/dev` prints nothing —
each branch has its own root commit). So there is nothing to merge and no
common ancestor to diff. Moving code between branches means naming the branch
and the path.

| branch | the service | what is on it | the command that runs there |
| --- | --- | --- | --- |
| `main` | — (the map) | `.bin/` toolkit, `AGENTS.md`, `PROJECTSTRUCTURE.md`, one placeholder dir per service | nothing — **no application code lives here** |
| `site` | site web, backend | `backend/{cookie,db,file,gateway}`, `backend/mode.js` | `node backend/db/db_test.js` (see the warning in PROJECTSTRUCTURE.md) |
| `dev` | staging | root `index.js` (the site entry), `frontend/` (built SPA), `Staging_architecture/` (the old engine), `deploy/cloudflare/` | none — `index.js` imports `./Backend/**`, which is on `site` |
| `architecture` | engine, production | `Bridge/api-server.js`, `Lexical_No_inspect/`, `MonoSyllabism/`, `PIPELINE.md` | `npm install && npm run api` |
| `Spa` | frontend source | `frontend/{index.html,js,scss,img}`, `frontend/vite.config.js` | `npm install && npm run build` (currently fails — see PROJECTSTRUCTURE.md) |
| `working` | praser | `Praser/Python/` (baseline + oracle), `Praser/JS/` (Node port), `Praser/fonts/` | `cd Praser/Python && python3 module/prase.py --help` |

`main` also holds a placeholder directory per service, and each one names the
branch it stands for:

| dir on `main` | branch |
| --- | --- |
| `P-Engine/architecture/`, `R_architecture/` | `architecture` |
| `P-Engine/dev_deploy/`, `R_dev/`, `dev/` | `dev` |
| `P-Site/Spa_frontend/`, `Spa/` | `Spa` |
| `P-Site/working_praser/`, `working/` | `working` |

## Trap 1 — your clone probably only has one branch

`git clone --single-branch` (the Codespaces and agent-sandbox default) leaves
`git branch -r` listing exactly one branch:

```console
$ git branch -a          # a real single-branch clone of `dev`
* dev
  remotes/origin/dev
$ git config --get-all remote.origin.fetch
+refs/heads/dev:refs/remotes/origin/dev
```

So a file that exists on `working` reads as a file that does not exist, and the
natural next step is to write it again. **Do not.** Make the other branches
visible first:

```bash
git config --add remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'
git fetch origin --prune
git branch -r
```

`./.bin/branch.sh doctor` reports this and `doctor --fix` does the two commands
above. Then, instead of searching the working tree, ask the branches directly:

```bash
./.bin/branch.sh where backend/mode.js        # → site
./.bin/branch.sh ls working Praser/JS         # what the praser's Node port holds
./.bin/branch.sh show architecture PIPELINE.md
./.bin/branch.sh get working Praser/JS/module/prase.mjs   # copy in, uncommitted
```

Or with plain git, from any branch:

```bash
git ls-tree -r --name-only origin/working      # every file on a branch
git show origin/site:backend/mode.js           # one file, no checkout
git checkout origin/working -- Praser/JS/module/paths.mjs   # copy into the tree
```

## Trap 2 — a path in a document belongs to the branch that wrote it

Every branch carries its own `AGENTS.md` / `PROJECTSTRUCTURE.md` /
`CHANGELOG.md`, and each one drifts: `dev`'s docs talk about `Site/` while the
tree holds `index.js` + `frontend/` + `Staging_architecture/`; `working`'s talk
about `R_Site/Praser/` while the tree holds `Praser/`. Trust the tree, not the
prose — `git ls-tree -r --name-only origin/<branch>` is the source of truth,
and `PROJECTSTRUCTURE.md` here is the only document that is meant to cover all
six branches at once.

## Running tests

There is no repo-wide `npm test` that works, on any branch, and that is a fact
about the layout rather than an oversight: each branch holds one service, and
the test files import modules from the *other* services. `PROJECTSTRUCTURE.md`
has the per-branch table with the exact failure each one produces, so you can
tell "my change broke it" from "this branch was never able to run it".
