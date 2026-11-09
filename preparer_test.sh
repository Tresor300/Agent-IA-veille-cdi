#!/bin/bash
# Copie fraîche de la mémoire de production (dépôt privé) dans data_test/, pour tester sans risque.
set -e
cd "$(dirname "$0")"
rm -rf data_test
git clone --quiet --depth 1 https://github.com/Tresor300/Agent-IA-veille-cdi-data.git data_test
rm -rf data_test/.git      # la copie ne peut jamais être renvoyée vers la production
echo "✅ Copie de test prête dans data_test/ : $(ls data_test | wc -l) fichier(s)"