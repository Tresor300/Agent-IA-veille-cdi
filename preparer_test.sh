#!/bin/bash
# Copie fraîche de la mémoire de production dans data_test/, pour tester sans risque.
set -e
cd "$(dirname "$0")"
rm -rf data_test
git clone --quiet --depth 1 https://github.com/Tresor300/Agent-IA-veille-alternance-data.git data_test
rm -rf data_test/.git      # la copie ne peut jamais être renvoyée vers la production
echo "✅ Copie de test prête dans data_test/ ($(du -h data_test/veilleur.db | cut -f1))"