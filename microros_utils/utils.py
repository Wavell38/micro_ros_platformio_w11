import os
import subprocess
import sys
import shutil
from pathlib import Path
import locale

_MSVC_ENV_CACHE = None


def _looks_like_build_cmd(command: str) -> bool:
    c = command.lower()
    # Ce sont les commandes qui nécessitent réellement MSVC
    return ("colcon build" in c) or (" cmake" in c) or ("ninja" in c)

def _normalize_windows_env(env: dict | None) -> dict | None:
    if not sys.platform.startswith("win") or env is None:
        return env

    # noms canoniques utiles
    canonical = {
        "path": "PATH",
        "pathext": "PATHEXT",
        "pythonpath": "PYTHONPATH",
        "psmodulepath": "PSMODULEPATH",
        "ament_prefix_path": "AMENT_PREFIX_PATH",
        "cmake_prefix_path": "CMAKE_PREFIX_PATH",
        "colcon_prefix_path": "COLCON_PREFIX_PATH",
        "include": "INCLUDE",
        "lib": "LIB",
        "libpath": "LIBPATH",
        "temp": "TEMP",
        "tmp": "TMP",
        "comspec": "COMSPEC",
        "systemroot": "SystemRoot",
        "windir": "WINDIR",
    }

    out = {}
    for k, v in env.items():
        lk = k.lower()
        ck = canonical.get(lk, k.upper())
        out[ck] = v

    return out

def _find_vswhere() -> str | None:
    # 1) si vswhere est déjà dans PATH
    p = shutil.which("vswhere")
    if p and os.path.isfile(p):
        return p
    # 2) emplacement standard
    p = os.path.join(
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        "Microsoft Visual Studio", "Installer", "vswhere.exe"
    )
    return p if os.path.isfile(p) else None

def _vswhere_install_paths(vswhere: str) -> list[str]:
    r = subprocess.run(
        [vswhere, "-all", "-products", "*", "-property", "installationPath"],
        capture_output=True, text=True, errors="replace"
    )
    return [line.strip() for line in (r.stdout or "").splitlines() if line.strip()]

def _filter_out_mingw_paths(path_value: str) -> str:
    bad_markers = [
        r"\winlibs\mingw",
        r"\mingw64\bin",
        r"\msys64\mingw",
        r"\msys64\ucrt64",
        r"\msys64\clang64",
    ]
    parts = []
    for p in path_value.split(os.pathsep):
        pl = p.lower().replace("/", "\\")
        if any(m in pl for m in bad_markers):
            continue
        parts.append(p)
    return os.pathsep.join(parts)


def run_proc(args, env=None, cwd=None):
    enc = locale.getpreferredencoding(False)
    env = _normalize_windows_env(env)
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding=enc,
        errors="replace",
        env=env,
        cwd=cwd
    )

def _candidate_vsdevcmd_paths() -> list[str]:
    # Override explicite si tu veux stabiliser à 100%
    override = os.environ.get("MICROROS_VSDEVCMD")
    if override and os.path.isfile(override):
        return [override]

    cands = []

    # Cas le plus fréquent
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")

    # Noms d'éditions possibles
    editions = ["BuildTools", "Community", "Professional", "Enterprise"]
    years = ["2022", "2019"]

    for base in (pf, pfx86):
        for y in years:
            for e in editions:
                cands.append(os.path.join(base, "Microsoft Visual Studio", y, e, "Common7", "Tools", "VsDevCmd.bat"))

    # Fallback registry (si installation non standard)
    # (ça marche même sans vswhere)
    for key in (
        r"HKLM\SOFTWARE\Microsoft\VisualStudio\SxS\VS7",
        r"HKLM\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\SxS\VS7",
    ):
        try:
            r = subprocess.run(["reg", "query", key], capture_output=True, text=True, errors="replace")
            for line in (r.stdout or "").splitlines():
                # ligne typique: "17.0    REG_SZ    C:\Program Files\Microsoft Visual Studio\2022\BuildTools\"
                if "REG_SZ" in line:
                    install = line.split("REG_SZ", 1)[1].strip()
                    cands.append(os.path.join(install, "Common7", "Tools", "VsDevCmd.bat"))
                    cands.append(os.path.join(install, "VC", "Auxiliary", "Build", "vcvarsall.bat"))

        except Exception:
            pass

    return cands


def _find_vsdevcmd_bat() -> str | None:
    override = os.environ.get("MICROROS_VSDEVCMD")
    if override and os.path.isfile(override):
        return override

    vswhere = _find_vswhere()
    if vswhere:
        paths = _vswhere_install_paths(vswhere)

        # Préférer BuildTools si présent
        def pref(p: str) -> int:
            pl = p.lower()
            return 0 if pl.endswith("\\buildtools") or "\\buildtools" in pl else 1
        paths = sorted(paths, key=pref)

        for ip in paths:
            # 1) VsDevCmd
            p = os.path.join(ip, "Common7", "Tools", "VsDevCmd.bat")
            if os.path.isfile(p):
                return p

            # 2) LaunchDevCmd (souvent présent)
            p = os.path.join(ip, "Common7", "Tools", "LaunchDevCmd.bat")
            if os.path.isfile(p):
                return p

            # 3) fallback minimal : vcvarsall
            p2 = os.path.join(ip, "VC", "Auxiliary", "Build", "vcvarsall.bat")
            if os.path.isfile(p2):
                return p2

    # Fallback global (chemins standards + registry)
    for p in _candidate_vsdevcmd_paths():
        if os.path.isfile(p):
            return p

    return None

def _load_msvc_env() -> dict | None:
    global _MSVC_ENV_CACHE
    if _MSVC_ENV_CACHE is not None:
        return _MSVC_ENV_CACHE

    if not sys.platform.startswith("win"):
        _MSVC_ENV_CACHE = None
        return None

    vsdevcmd = _find_vsdevcmd_bat()
    # Normalisation du chemin (évite les \" qui cassent cmd.exe)
    vsdevcmd = vsdevcmd.strip().strip('"')
    vsdevcmd = vsdevcmd.replace('\\\"', '"')
    
    if not vsdevcmd:
        _MSVC_ENV_CACHE = None
        return None

    arch = os.environ.get("MICROROS_MSVC_ARCH", "amd64")
    host = os.environ.get("MICROROS_MSVC_HOST", "amd64")

    # charge l'env et dump via `set`
    if vsdevcmd.lower().endswith("vcvarsall.bat"):
        vc_arch = "x64" if arch.lower() in ("amd64", "x64") else "x86"
        cmd = f'call "{vsdevcmd}" {vc_arch} && set'
    else:
        cmd = f'call "{vsdevcmd}" -arch={arch} -host_arch={host} && set'

    print("MSVC BAT =", vsdevcmd)
    print("CMD      =", cmd)

    comspec = os.environ.get("COMSPEC", "cmd.exe")

    if vsdevcmd.lower().endswith("vcvarsall.bat"):
        vc_arch = "x64" if arch.lower() in ("amd64", "x64") else "x86"
        cmdline = f'{comspec} /d /s /c ""{vsdevcmd}" {vc_arch} && set"'
    else:
        cmdline = f'{comspec} /d /s /c ""{vsdevcmd}" -arch={arch} -host_arch={host} && set"'

    # Debug (optionnel)
    print("CMDLINE  =", cmdline)

    r = subprocess.run(cmdline, capture_output=True, text=True, errors="replace")
    
    if r.returncode != 0:
        return {"__MICROROS_VSDEVCMD_FAILED__": (r.stderr or "").strip()}

    env = {}
    for line in (r.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            env[k] = v

    # sanity check
    if "VCToolsInstallDir" not in env and "VCINSTALLDIR" not in env:
        return {"__MICROROS_MSVC_ENV_INCOMPLETE__": "1"}
    
    def _env_has_exe(env: dict, exe: str) -> bool:
        for p in env.get("PATH", "").split(os.pathsep):
            if os.path.isfile(os.path.join(p, exe)):
                return True
        return False
    
    if not _env_has_exe(env, "cl.exe"):
        # VS détecté mais outils C++ non présents (ou workload incomplet)
        return {"__MICROROS_MSVC_PRESENT_BUT_NO_CL__": "1"}  # marqueur

    env = _normalize_windows_env(env)
    _MSVC_ENV_CACHE = env
    return env


def _find_git_root() -> Path | None:
    # Override optionnel (utile si installation exotique)
    override = os.environ.get("MICROROS_GIT_ROOT")
    if override:
        p = Path(override)
        if (p / "usr" / "bin" / "bash.exe").is_file():
            return p

    # Emplacements standards Git for Windows
    candidates = []
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    candidates += [Path(pf) / "Git", Path(pfx86) / "Git"]

    # Fallback: si git.exe est dans PATH, déduire root
    git = shutil.which("git")
    if git:
        # ...\Git\cmd\git.exe ou ...\Git\bin\git.exe
        root = Path(git).resolve().parent.parent
        candidates.insert(0, root)

    for root in candidates:
        if (root / "usr" / "bin" / "bash.exe").is_file():
            return root

    return None

def _inject_git_paths(env: dict) -> None:
    root = _find_git_root()
    if not root:
        return

    # Ces répertoires couvrent les installs Git for Windows classiques
    prepend = [
        str(root / "cmd"),
        str(root / "bin"),          # chez toi, git.exe est ici
        str(root / "usr" / "bin"),
        str(root / "mingw64" / "bin"),
    ]

    cur = env.get("PATH", "")
    env["PATH"] = os.pathsep.join(prepend + ([cur] if cur else []))


def _find_git_bash() -> str | None:
    # Override explicite
    override = os.environ.get("MICROROS_BASH")
    if override and os.path.isfile(override):
        return override

    root = _find_git_root()
    if root:
        return str(root / "usr" / "bin" / "bash.exe")

    # Dernier fallback: bash dans PATH, mais refuser WSL shim
    bash = shutil.which("bash")
    if bash:
        b = os.path.normcase(os.path.normpath(bash))
        if b.endswith(r"\windows\system32\bash.exe"):
            return None
        return bash

    return None

def load_env_from_bat(bat_path: str, args: str = "") -> tuple[dict, str] | tuple[None, str]:
    """
    Exécute un .bat dans un cmd.exe temporaire et renvoie l'environnement résultant via `set`.
    Retour: (env_dict, "") si OK, sinon (None, stderr).
    """
    bat = (bat_path or "").strip().strip('"').replace('\\"', '"')
    # cmd.exe aime les backslashes
    bat = bat.replace("/", "\\")
    comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")

    # /c ""<bat>" <args> && set"
    cmdline = f'{comspec} /d /s /c ""{bat}" {args} && set"'
    r = subprocess.run(cmdline, capture_output=True, text=True, errors="replace")

    if r.returncode != 0:
        return None, (r.stderr or "").strip()

    env = {}
    for line in (r.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            env[k] = v

    env = _normalize_windows_env(env)
    return env, ""


def run_cmd_win(command: str, env=None, cwd=None):
    """
    Exécute une commande dans cmd.exe (Windows) et retourne stdout/stderr en texte.
    """
    if not sys.platform.startswith("win"):
        return subprocess.run(command, capture_output=True, shell=True, text=True, errors="replace", env=env, cwd=cwd)

    comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
    cmdline = f'{comspec} /d /s /c ""{command}""'

    # Encodage local Windows (FR -> cp1252 typiquement). Ne jamais crasher.
    enc = locale.getpreferredencoding(False)
    return subprocess.run(
        cmdline,
        capture_output=True,
        text=True,
        encoding=enc,
        errors="replace",
        env=env,
        cwd=cwd
    )

def run_cmd(command, env=None):
    if sys.platform.startswith("win"):
        
        bash = _find_git_bash()
        if not bash:
            raise RuntimeError(
                "Git Bash introuvable. Installe Git for Windows ou définis MICROROS_BASH vers ...\\Git\\usr\\bin\\bash.exe"
            )

        base = os.environ.copy()
        if env:
            base.update(env)

        _inject_git_paths(base)

        # MSYS/Git Bash : hériter du PATH Windows
        base.setdefault("CHERE_INVOKING", "1")
        base.setdefault("MSYS2_PATH_TYPE", "inherit")

        toolchain = os.environ.get("MICROROS_TOOLCHAIN", "msvc").lower()

        # IMPORTANT: on n'exige MSVC que pour les commandes de build.
        def _is_cross_compile_cmd(command: str) -> bool:
            return "-dcmake_toolchain_file=" in command.lower()

        if toolchain == "msvc" and _looks_like_build_cmd(command) and not _is_cross_compile_cmd(command):
            msvc_env = _load_msvc_env()
            if not msvc_env:
                raise RuntimeError("Impossible de charger l'environnement via le .bat MSVC (retour None).")

            if "__MICROROS_VSDEVCMD_FAILED__" in msvc_env:
                raise RuntimeError(
                    "Le .bat MSVC a été trouvé mais son exécution a échoué (cmd.exe non-zero). "
                    f"stderr: {msvc_env['__MICROROS_VSDEVCMD_FAILED__']}"
                )
            if "__MICROROS_MSVC_ENV_INCOMPLETE__" in msvc_env:
                raise RuntimeError(
                    "Le .bat MSVC a été trouvé/exécuté mais l'environnement est incomplet "
                    "(VCTools/VCINSTALLDIR absent). Workload C++ probablement incomplet."
                )
            if "__MICROROS_MSVC_PRESENT_BUT_NO_CL__" in msvc_env:
                raise RuntimeError(
                    "MSVC détecté mais cl.exe introuvable dans PATH après initialisation. "
                    "Workload C++ (VCTools) manquant ou arch incorrecte."
                )
            base.update(msvc_env)
        
        # IMPORTANT: éviter que CMake prenne ld.exe (MinGW) comme linker
        base["PATH"] = _filter_out_mingw_paths(base.get("PATH", ""))

        # Optionnel : neutraliser des variables qui peuvent orienter vers GNU tools
        base.pop("LD", None)
        base.pop("CC", None)
        base.pop("CXX", None)

        return subprocess.run([bash, "-lc", command], capture_output=True, env=base)

    return subprocess.run(command, capture_output=True, shell=True, env=env)