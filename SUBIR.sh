#!/usr/bin/env bash
# Sube este proyecto al repo de GitHub. Corre esto desde la carpeta nfl-edge/
# Uso:  bash SUBIR.sh
set -e
REPO="https://github.com/moypenhos1610/nfl-edge26.git"
git init -q 2>/dev/null || true
git add -A
git -c user.name="$(git config user.name || echo moypenhos1610)" commit -qm "NFL EDGE 2026" || true
git branch -M main
git remote remove origin 2>/dev/null || true
git remote add origin "$REPO"
echo "Empujando a $REPO … (GitHub te pedirá tu usuario y un token personal)"
git push -u --force origin main
echo
echo "Listo. Ahora activa Pages:  Settings -> Pages -> Source: main, carpeta /docs"
echo "Y lanza la primera corrida:  Actions -> Actualizar NFL EDGE -> Run workflow"
