#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "Usage: sh packaging/install.sh PATH_TO_MEDIADL_BINARY" >&2
  exit 2
fi

SOURCE=$1
if [ ! -f "$SOURCE" ]; then
  echo "MediaDL binary not found: $SOURCE" >&2
  exit 2
fi

BIN_DIR=${XDG_BIN_HOME:-"$HOME/.local/bin"}
TARGET="$BIN_DIR/mdl"
TEMP="$BIN_DIR/.mdl.install.$$"
mkdir -p "$BIN_DIR"
trap 'rm -f "$TEMP"' EXIT HUP INT TERM
cp "$SOURCE" "$TEMP"
chmod 755 "$TEMP"
"$TEMP" --version >/dev/null
mv -f "$TEMP" "$TARGET"
trap - EXIT HUP INT TERM

case ":${PATH:-}:" in
  *:"$BIN_DIR":*) ;;
  *)
    PROFILE="$HOME/.profile"
    MARKER='# MediaDL user binary path'
    if [ ! -f "$PROFILE" ] || ! grep -Fq "$MARKER" "$PROFILE"; then
      ESCAPED_BIN_DIR=$(printf '%s' "$BIN_DIR" | sed 's/[\\`"$]/\\&/g')
      {
        printf '\n%s\n' "$MARKER"
        printf 'export PATH="%s:$PATH"\n' "$ESCAPED_BIN_DIR"
      } >> "$PROFILE"
    fi
    echo "PATH updated in $PROFILE for future terminals."
    echo "For this terminal now: export PATH=\"$BIN_DIR:\$PATH\""
    ;;
esac

echo "MediaDL installed: $TARGET"
echo "Use: mdl --help"
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "Required: install FFmpeg before downloading so MediaDL can merge/convert media."
fi
