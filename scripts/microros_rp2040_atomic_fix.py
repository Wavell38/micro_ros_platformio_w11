from pathlib import Path
import shutil
import subprocess
import re

Import("env")

BUILD_DIR = Path(env.subst("$BUILD_DIR"))
LINK_TARGET = env.subst("$BUILD_DIR/${PROGNAME}.elf")

PATCH_DIR = BUILD_DIR / "microros_patched"
PATCHED_LIB = PATCH_DIR / "libmicroros.a"


def _find_original_libmicroros() -> Path:
    for entry in env.get("LIBPATH", []):
        p = Path(str(entry))
        candidate = p / "libmicroros.a"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "libmicroros.a introuvable dans LIBPATH. "
        f"LIBPATH={env.get('LIBPATH', [])}"
    )


ORIGINAL_LIB = _find_original_libmicroros()

# Très important : on met notre dossier patché en tête pour CET environnement uniquement
env.PrependUnique(LIBPATH=[str(PATCH_DIR)])


def _run(cmd):
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Commande échouée:\n"
            + " ".join(map(str, cmd))
            + "\n\nSTDOUT:\n"
            + (result.stdout or "")
            + "\nSTDERR:\n"
            + (result.stderr or "")
        )
    return result.stdout


def _find_tool(name: str) -> str:
    tool = shutil.which(name)
    if not tool:
        raise FileNotFoundError(f"Outil introuvable dans PATH: {name}")
    return tool


def _find_atomic_member(lib_path: Path) -> str | None:
    nm = _find_tool("arm-none-eabi-nm")
    out = _run([nm, "-A", str(lib_path)])

    # Exemple attendu :
    # libmicroros.a:o0000653.obj:00000000 T __atomic_load_8
    pattern = re.compile(
        r":(?P<member>[^:()]+\.obj):[0-9A-Fa-f]+\s+(?P<kind>[A-Za-z])\s+__atomic_load_8$"
    )

    for line in out.splitlines():
        m = pattern.search(line.strip())
        if not m:
            continue
        kind = m.group("kind").upper()
        if kind != "U":  # U = undefined, donc pas une définition
            return m.group("member")

    return None


def _prepare_patched_lib(target, source, env):
    PATCH_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ORIGINAL_LIB, PATCHED_LIB)

    member = _find_atomic_member(PATCHED_LIB)
    if not member:
        print("[microros-rp2040-fix] Aucun shim atomique détecté, copie non modifiée.")
        print(f"[microros-rp2040-fix] Using copied lib: {PATCHED_LIB}")
        return

    ar = _find_tool("arm-none-eabi-ar")
    print(f"[microros-rp2040-fix] Removing atomic shim member: {member}")
    _run([ar, "dv", str(PATCHED_LIB), member])
    print(f"[microros-rp2040-fix] Using patched lib: {PATCHED_LIB}")


# On prépare la copie juste avant l'édition de liens
env.AddPreAction(LINK_TARGET, _prepare_patched_lib)