#!/usr/bin/env bash
# Compila CXT e instala los scripts de ejecución en CXT_DIR (por defecto ~/.cxt).
set -euo pipefail
cd "$(dirname "$0")"

DEST="${CXT_DIR:-$HOME/.cxt}"

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "falta: $1" >&2; exit 1; }
}
need go
need python3

echo "[1/3] compilando..."
bash build.sh

echo "[2/3] instalando runtime en $DEST ..."
mkdir -p "$DEST/bin" "$DEST/fixtures"

same_file() {
  [ -e "$2" ] && [ "$(realpath "$1")" = "$(realpath "$2")" ]
}

copy_if_needed() {
  local src="$1" dst="$2"
  [ -e "$src" ] || return 0
  mkdir -p "$(dirname "$dst")"
  if ! same_file "$src" "$dst"; then
    if [ -d "$src" ]; then
      mkdir -p "$dst"
      cp -Rf "$src"/. "$dst"/
    else
      cp -f "$src" "$dst"
    fi
  fi
}

# Scripts de ejecución (los tests y utilidades de desarrollo no se instalan).
for f in *.py *.sh; do
  [ -e "$f" ] || continue
  case "$f" in
    test_*|install.sh) continue ;;
  esac
  copy_if_needed "$PWD/$f" "$DEST/$f"
done

# Binarios que usa el scheduler y el CLI.
for f in bin/cxt bin/cxtd; do
  copy_if_needed "$PWD/$f" "$DEST/$f"
done

# Datos de prueba que usa research.py.
if [ -d fixtures ]; then
  for f in fixtures/*; do
    [ -e "$f" ] || continue
    copy_if_needed "$PWD/$f" "$DEST/$f"
  done
fi

chmod +x "$DEST"/*.sh "$DEST"/bin/cxt "$DEST"/bin/cxtd 2>/dev/null || true

echo "[3/3] listo. Siguientes pasos:"
cat <<EOF

  1) Arranca el puente:
       cd "$DEST"
       ./bin/cxtd

  2) Carga la extensión en Chrome:
       chrome://extensions -> Modo de desarrollador -> Cargar descomprimida
       -> $PWD/extension

  3) Comprueba:
       cd "$DEST"
       ./bin/cxt status

  Si DEST no es ~/.cxt, revisa las rutas fijas del scheduler y de la mesa editorial
  (ver README, sección "Archivos de estado").

EOF
