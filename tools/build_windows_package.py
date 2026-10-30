"""Build the OFFLINE Windows package: the agent and every library it needs, for
Windows 64-bit and Python 3.10 to 3.13, in one zip. Sites rarely have internet.

    python tools/build_windows_package.py            -> dist/agent-surveillance-puits-windows.zip

On the site PC: unzip, (optionally put python-3.12.x-amd64.exe in the install
folder), right-click install\\install-windows.bat > Run as administrator.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYS = ("3.10", "3.11", "3.12", "3.13")


def main():
    out = os.path.join(ROOT, "dist")
    os.makedirs(out, exist_ok=True)
    tmp = tempfile.mkdtemp()
    wheels = os.path.join(tmp, "wheels")
    for v in PYS:
        subprocess.run([sys.executable, "-m", "pip", "download", "-q", "--only-binary=:all:", "--platform", "win_amd64",
                        "--python-version", v, "--implementation", "cp", "-d", wheels, "-r", os.path.join(ROOT, "requirements.txt")],
                       check=True)
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    files = [f for f in files if not f.startswith(("tests/", "docs/img/", ".github/")) and f != "index.html" and os.path.exists(os.path.join(ROOT, f))]
    zp = os.path.join(out, "agent-surveillance-puits-windows.zip")
    top = "agent-surveillance-puits"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(os.path.join(ROOT, f), f"{top}/{f}")
        for w in sorted(os.listdir(wheels)):
            z.write(os.path.join(wheels, w), f"{top}/wheels/{w}")
        z.writestr(f"{top}/LISEZ-MOI.txt", README)
    shutil.rmtree(tmp)
    print(f"{zp}  ({os.path.getsize(zp) / 1e6:.1f} MB, {len(files)} files + {len(os.listdir(wheels)) if os.path.exists(wheels) else 'n'} wheels)")


README = """AGENT DE SURVEILLANCE DES PUITS - INSTALLATION SUR WINDOWS (sans internet)

1. Copier ce dossier sur le PC, par exemple C:\\agent-surveillance-puits
2. Si Python 3.10 a 3.13 n'est pas installe : demander a l'informatique le fichier
   python-3.12.x-amd64.exe et le mettre dans le dossier "install".
3. Clic droit sur install\\install-windows.bat > Executer en tant qu'administrateur.
4. Le navigateur ouvre l'assistant de mise en service (http://127.0.0.1:8080/setup) :
   adresse OPC UA du SCADA, compte en lecture seule, l'agent reconnait les puits,
   liste d'astreinte, SMS, e-mail, compte superviseur. Enregistrer : la surveillance demarre.
5. Pour charger l'historique : tableau de bord > onglet Donnees.

L'agent ne fait que LIRE le SCADA. Guide complet : docs/INSTALL.fr.md
"""

if __name__ == "__main__":
    main()
