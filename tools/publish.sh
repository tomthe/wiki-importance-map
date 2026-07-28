#!/bin/sh
# Publish the site plus data/ to the gh-pages branch, as ONE root commit.
#
# master carries the source and its history; data/ is gitignored there. This
# script pushes a commit with *no parent*, so the branch is replaced wholesale
# every time and the remote holds exactly one copy of the data no matter how
# often the pipeline is rerun. That is the only thing that keeps a repository
# whose payload is half a gigabyte from growing without bound - see the
# "One thing Pages still cannot do is forget" section of the README.
#
# It works through a temporary index (GIT_INDEX_FILE) and git plumbing, so it
# never checks a branch out and never touches your working tree: data/ can stay
# exactly where the pipeline left it.
#
#   sh tools/publish.sh          # push
#   sh tools/publish.sh --dry-run # build the commit, print it, push nothing
#
# Point GitHub Pages at the gh-pages branch, root directory, once.

set -e

cd "$(dirname "$0")/.."
root=$(pwd)

dry=""
[ "$1" = "--dry-run" ] && dry=1

if [ ! -f data/manifest.json ]; then
    echo "data/manifest.json is missing - run the pipeline first" >&2
    exit 1
fi

# GitHub refuses any single file over 100 MiB on push. build_tiles caps the pack
# parts at 90 MiB, but a hand-run with --part-bytes would sail past it and the
# rejection comes only after uploading everything.
limit=$((100 * 1024 * 1024))
for f in data/*; do
    size=$(wc -c <"$f")
    if [ "$size" -ge "$limit" ]; then
        echo "$f is $size bytes, over GitHub's 100 MiB single-file limit" >&2
        exit 1
    fi
done

index="$root/$(git rev-parse --git-dir)/publish.idx"
rm -f "$index"

# -f because data/ is ignored on master, which is the point.
GIT_INDEX_FILE="$index" git add -f .nojekyll index.html src data
tree=$(GIT_INDEX_FILE="$index" git write-tree)
rm -f "$index"

# No -p: an orphan commit. The previous gh-pages tip becomes unreachable.
commit=$(git commit-tree "$tree" -m "publish $(date +%F)")

bytes=$(du -sk data | cut -f1)
echo "commit $commit  tree $tree  (${bytes} KiB of data)"

if [ -n "$dry" ]; then
    echo "--dry-run: not pushing. Inspect it with:"
    echo "  git ls-tree -r --long $commit"
    exit 0
fi

git push -f origin "$commit:refs/heads/gh-pages"
echo "pushed to gh-pages. https://tomthe.github.io/wikipediapagerankmap/"
