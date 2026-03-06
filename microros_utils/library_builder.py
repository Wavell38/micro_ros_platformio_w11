import os, sys, re
import yaml
import shutil
import subprocess, glob
from pathlib import Path
import shutil
import itertools

from .utils import run_cmd, run_proc, load_env_from_bat, _normalize_windows_env, _load_msvc_env
from .repositories import Repository, Sources

def _slash(p: str) -> str:
    # Bash/MSYS: éviter les backslashes (échappements)
    return p.replace("\\", "/") if sys.platform.startswith("win") else p

def _sanitize_gcc_flags_for_windows(flags: str) -> str:
    # 1) enlever les quotes simples autour des -D'...'
    flags = re.sub(r"-D'([^']+)'", r"-D\1", flags)

    # 2) enlever la rustine MSVC qui casse sous cmd.exe et qui ne doit pas être appliquée à GCC
    #    (ça couvre -D__attribute__(x)=, -D"__attribute__(x)=", etc.)
    flags = re.sub(r"-D\"?__attribute__\([^)]*\)=\"?", "", flags)

    # 3) clean espaces
    flags = re.sub(r"\s{2,}", " ", flags).strip()
    return flags

# ---- helpers (local scope, avoids missing definitions) ----
def _chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i+n]

def _find_binutils() -> tuple[str, str]:
    """
    Trouve arm-none-eabi-ar / arm-none-eabi-ranlib de manière robuste.
    - overrides via env MICROROS_AR / MICROROS_RANLIB
    - via PATH
    - fallback PlatformIO: ~/.platformio/packages/**/bin/(arm-none-eabi-ar.exe, arm-none-eabi-ranlib.exe)
    """
    ar = os.environ.get("MICROROS_AR")
    ranlib = os.environ.get("MICROROS_RANLIB")
    if ar and ranlib and Path(ar).is_file() and Path(ranlib).is_file():
        return ar, ranlib

    # PATH
    ar = shutil.which("arm-none-eabi-ar") or shutil.which("arm-none-eabi-ar.exe")
    ranlib = shutil.which("arm-none-eabi-ranlib") or shutil.which("arm-none-eabi-ranlib.exe")
    if ar and ranlib:
        return ar, ranlib

    # PlatformIO packages dir
    pio_packages = os.environ.get("PLATFORMIO_PACKAGES_DIR")
    if pio_packages:
        base = Path(pio_packages)
    else:
        base = Path.home() / ".platformio" / "packages"

    if base.is_dir():
        # scan "bin" folders under packages
        for bin_dir in base.glob("**/bin"):
            ar2 = bin_dir / "arm-none-eabi-ar.exe"
            ran2 = bin_dir / "arm-none-eabi-ranlib.exe"
            if ar2.is_file() and ran2.is_file():
                return str(ar2), str(ran2)

    raise RuntimeError(
        "Binutils introuvables: arm-none-eabi-ar / arm-none-eabi-ranlib.\n"
        "Solutions: (a) mettre la toolchain dans PATH, (b) définir MICROROS_AR et MICROROS_RANLIB."
    )

def _default_pathext() -> str:
    # PATHEXT standard Windows (minimal utile)
    return ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC"

def _ensure_pathext(env: dict) -> None:
    pe = env.get("PATHEXT") or ""
    if not pe:
        env["PATHEXT"] = _default_pathext()
        return
    up = pe.upper()
    # s'assurer que .BAT et .CMD existent
    add = []
    if ".BAT" not in up: add.append(".BAT")
    if ".CMD" not in up: add.append(".CMD")
    if add:
        env["PATHEXT"] = pe.rstrip(";") + ";" + ";".join(add)

def _write_ament_wrappers(dirpath: str) -> None:
    os.makedirs(dirpath, exist_ok=True)

    # 1) ament_append_value VAR VALUE  -> append avec ';'
    append_bat = r"""@echo off
        setlocal EnableExtensions EnableDelayedExpansion
        set "VAR=%~1"
        set "VAL=%~2"
        if "%VAR%"=="" exit /b 2
        set "CUR=!%VAR%!"
        if not "!CUR!"=="" set "CUR=!CUR!;"
        set "%VAR%=!CUR!!VAL!"
        exit /b 0
        """
    # 2) ament_prepend_unique_value VAR VALUE -> prepend si absent (split simple sur ';')
    prepend_unique_bat = r"""@echo off
        setlocal EnableExtensions EnableDelayedExpansion
        set "VAR=%~1"
        set "VAL=%~2"
        if "%VAR%"=="" exit /b 2
        set "CUR=!%VAR%!"
        if "!CUR!"=="" (
        set "%VAR%=!VAL!"
        exit /b 0
        )
        set "HAY=;!CUR!;"
        echo(!HAY! | findstr /I /C:";!VAL!;" >nul
        if errorlevel 1 (
        set "%VAR%=!VAL!;!CUR!"
        )
        exit /b 0
        """
    # 3) ament_append_unique_value VAR VALUE -> append si absent
    append_unique_bat = r"""@echo off
        setlocal EnableExtensions EnableDelayedExpansion
        set "VAR=%~1"
        set "VAL=%~2"
        if "%VAR%"=="" exit /b 2
        set "CUR=!%VAR%!"
        if "!CUR!"=="" (
        set "%VAR%=!VAL!"
        exit /b 0
        )
        set "HAY=;!CUR!;"
        echo(!HAY! | findstr /I /C:";!VAL!;" >nul
        if errorlevel 1 (
        set "%VAR%=!CUR!;!VAL!"
        )
        exit /b 0
        """

    with open(os.path.join(dirpath, "ament_append_value.bat"), "w", newline="\r\n", encoding="utf-8") as f:
        f.write(append_bat)
    with open(os.path.join(dirpath, "ament_prepend_unique_value.bat"), "w", newline="\r\n", encoding="utf-8") as f:
        f.write(prepend_unique_bat)
    with open(os.path.join(dirpath, "ament_append_unique_value.bat"), "w", newline="\r\n", encoding="utf-8") as f:
        f.write(append_unique_bat)

class CMakeToolchain:
    def __init__(self, path, cc, cxx, ar, cflags, cxxflags):
        cmake_toolchain = """include(CMakeForceCompiler)
            set(CMAKE_SYSTEM_NAME Generic)

            set(CMAKE_CROSSCOMPILING 1)
            set(CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY)

            SET (CMAKE_C_COMPILER_WORKS 1)
            SET (CMAKE_CXX_COMPILER_WORKS 1)

            set(CMAKE_C_COMPILER {C_COMPILER})
            set(CMAKE_CXX_COMPILER {CXX_COMPILER})
            set(CMAKE_AR {AR_COMPILER})

            set(CMAKE_C_FLAGS_INIT "{C_FLAGS}" CACHE STRING "" FORCE)
            set(CMAKE_CXX_FLAGS_INIT "{CXX_FLAGS}" CACHE STRING "" FORCE)

            set(__BIG_ENDIAN__ 0)"""
        
        if sys.platform.startswith("win"):
            cflags = _sanitize_gcc_flags_for_windows(cflags)
            cxxflags = _sanitize_gcc_flags_for_windows(cxxflags)

        cmake_toolchain = cmake_toolchain.format(C_COMPILER=cc, CXX_COMPILER=cxx, AR_COMPILER=ar, C_FLAGS=cflags, CXX_FLAGS=cxxflags)

        with open(path, "w") as file:
            file.write(cmake_toolchain)

        self.path = os.path.realpath(file.name)

class Build:
    def __init__(self, library_folder, packages_folder, distro, python_env):
        self.library_folder = _slash(library_folder)
        self.packages_folder = _slash(packages_folder)
        self.build_folder = self.library_folder + "/build"
        self.distro = distro

        self.dev_packages = []
        self.mcu_packages = []

        self.dev_folder = self.build_folder + '/dev'
        self.dev_src_folder = self.dev_folder + '/src'
        self.mcu_folder = self.build_folder + '/mcu'
        self.mcu_src_folder = self.mcu_folder + '/src'

        self.library_path = library_folder + '/libmicroros'
        self.library = self.library_path + "/libmicroros.a"
        self.includes = self.library_path+ '/include'
        self.library_name = "microros"
        self.python_env = _slash(python_env)
        self.env = {}

    def run(self, meta, toolchain, user_meta = ""):

        if os.path.exists(self.library):
            print("micro-ROS already built")
            return
        
        meta = _slash(meta)
        toolchain = _slash(toolchain)
        user_meta = _slash(user_meta)

        self.check_env()
        self.download_dev_environment()
        self.build_dev_environment()
        self.download_mcu_environment()
        self.build_mcu_environment(meta, toolchain, user_meta)
        self.package_mcu_library()

    def ignore_package(self, name):
        for p in self.mcu_packages:
            if p.name == name:
                p.ignore()

    def check_env(self):
        ROS_DISTRO = os.getenv('ROS_DISTRO')

        if (ROS_DISTRO):
            PATH = os.getenv('PATH')
            os.environ['PATH'] = PATH.replace('/opt/ros/{}/bin:'.format(ROS_DISTRO), '')
            os.environ.pop('AMENT_PREFIX_PATH', None)

        RMW_IMPLEMENTATION = os.getenv('RMW_IMPLEMENTATION')

        if (RMW_IMPLEMENTATION):
            os.environ['RMW_IMPLEMENTATION'] = "rmw_microxrcedds"

        self.env = os.environ.copy()


    def patch_microcdr_superbuild_windows(self, mcu_src_folder: str):
        if not sys.platform.startswith("win"):
            return

        path = os.path.join(mcu_src_folder, "micro-CDR", "cmake", "SuperBuild.cmake")
        if not os.path.isfile(path):
            return

        txt = open(path, "r", encoding="utf-8", errors="replace").read()
        patched = txt

        # 1) Forcer BINARY_DIR vers ucdr-build (remplacement, pas ajout)
        #    (idempotent : si déjà /ucdr-build, ça ne bouge pas)
        if not re.search(r"BINARY_DIR\s*\n\s*\$\{CMAKE_CURRENT_BINARY_DIR\}/ucdr-build", patched):
            # cas standard du fichier que tu as link
            patched2, n = re.subn(
                r"(ExternalProject_Add\(\s*ucdr\b.*?BINARY_DIR\s*\n\s*)\$\{CMAKE_CURRENT_BINARY_DIR\}(\s*\n)",
                r"\1${CMAKE_CURRENT_BINARY_DIR}/ucdr-build\2",
                patched,
                count=1,
                flags=re.S
            )
            if n > 0:
                patched = patched2
            else:
                # fallback simple si indentation différente
                patched = patched.replace(
                    "BINARY_DIR\n        ${CMAKE_CURRENT_BINARY_DIR}",
                    "BINARY_DIR\n        ${CMAKE_CURRENT_BINARY_DIR}/ucdr-build"
                )

        # 2) Injecter le toolchain dans CMAKE_CACHE_ARGS (idempotent)
        #    IMPORTANT : on évite de forcer CMAKE_C_COMPILER/CXX ici.
        #    Le toolchain file suffit et évite d’écrire "-DCMAKE_C_COMPILER=" si vide.
        if "CMAKE_TOOLCHAIN_FILE" not in patched:
            marker = "-DUCDR_SUPERBUILD:BOOL=OFF"
            inject = (
                marker + "\n"
                "        -DCMAKE_TOOLCHAIN_FILE:FILEPATH=${CMAKE_TOOLCHAIN_FILE}\n"
                "        -DCMAKE_BUILD_TYPE:STRING=${CMAKE_BUILD_TYPE}\n"
                "        -DCMAKE_MAKE_PROGRAM:FILEPATH=${CMAKE_MAKE_PROGRAM}"
            )
            patched = patched.replace(marker, inject, 1)

        if patched != txt:
            open(path, "w", encoding="utf-8", newline="\n").write(patched)

    def download_dev_environment(self):
        os.makedirs(self.dev_src_folder, exist_ok=True)
        print("Downloading micro-ROS dev dependencies")
        for repo in Sources.dev_environments[self.distro]:
            repo.clone(self.dev_src_folder)
            print("\t - Downloaded {}".format(repo.name))
            self.dev_packages.extend(repo.get_packages())

    def build_dev_environment(self):
        print("Building micro-ROS dev dependencies")

        install_prefix = f'{self.dev_folder}/install'

        # Fix build: Ignore rmw_test_fixture_implementation in rolling/kilted
        touch_rel = None
        if self.distro in ('rolling', 'kilted'):
            touch_rel = os.path.join(
                "src",
                "ament_cmake_ros",
                "rmw_test_fixture_implementation",
                "COLCON_IGNORE"
            )

        if sys.platform.startswith("win"):
            merged_env = dict(self.env) if self.env else {}

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
                    "(VCTools/VCINSTALLDIR absent)."
                )
            if "__MICROROS_MSVC_PRESENT_BUT_NO_CL__" in msvc_env:
                raise RuntimeError(
                    "MSVC détecté mais cl.exe introuvable dans PATH après initialisation."
                )

            merged_env.update(msvc_env)
            merged_env.setdefault("PYTHONUTF8", "1")
            merged_env.setdefault("PYTHONIOENCODING", "utf-8")
            _ensure_pathext(merged_env)

            dev_folder_win = self.dev_folder.replace("/", "\\")
            penv_scripts = os.path.dirname(self.python_env.replace("/", "\\"))
            python_exe_win = os.path.join(penv_scripts, "python.exe")
            python_exe_cmake = python_exe_win.replace("\\", "/")
            install_prefix_cmake = install_prefix.replace("\\", "/")

            if touch_rel:
                ignore_path = os.path.join(dev_folder_win, touch_rel)
                os.makedirs(os.path.dirname(ignore_path), exist_ok=True)
                open(ignore_path, "a").close()

            args = [
                python_exe_win, "-m", "colcon", "build",
                "--event-handlers", "console_direct+",
                "--cmake-args",
                "-G", "Ninja",
                "-DCMAKE_C_COMPILER=cl",
                "-DCMAKE_CXX_COMPILER=cl",
                "-DCMAKE_LINKER=link",
                "-DCMAKE_AR=lib",
                f"-DCMAKE_INSTALL_PREFIX:PATH={install_prefix_cmake}",
                "-DBUILD_TESTING=OFF",
                f"-DPython3_EXECUTABLE:FILEPATH={python_exe_cmake}",
            ]

            print("DEV COLCON ARGS =", args)
            result = run_proc(args, cwd=dev_folder_win, env=merged_env)

        else:
            touch_command = ''
            if self.distro in ('rolling', 'kilted'):
                touch_command = 'touch src/ament_cmake_ros/rmw_test_fixture_implementation/COLCON_IGNORE && '

            command = 'cd "{}" && {} . "{}" && colcon build --event-handlers desktop_notification- --cmake-args -G Ninja -DCMAKE_C_COMPILER=cl -DCMAKE_CXX_COMPILER=cl -DCMAKE_LINKER=link -DCMAKE_AR=lib -DCMAKE_INSTALL_PREFIX="{}" -DBUILD_TESTING=OFF -DPython3_EXECUTABLE=`which python`'.format(
                self.dev_folder, touch_command, self.python_env, install_prefix
            )
            result = run_cmd(command, env=self.env)

        if result.returncode != 0:
            print("\n--- STDERR --- Build dev micro-ROS environment failed: \n", result.stderr)
            sys.exit(1)

    def download_mcu_environment(self):
        os.makedirs(self.mcu_src_folder, exist_ok=True)
        print("Downloading micro-ROS library")
        for repo in Sources.mcu_environments[self.distro]:
            repo.clone(self.mcu_src_folder)
            self.mcu_packages.extend(repo.get_packages())
            for package in repo.get_packages():
                if package.name in Sources.ignore_packages[self.distro] or package.name.endswith("_cpp"):
                    package.ignore()

                print('\t - Downloaded {}{}'.format(package.name, " (ignored)" if package.ignored else ""))

        self.download_extra_packages()

    def download_extra_packages(self):
        if not os.path.exists(self.packages_folder):
            print("\t - Extra packages folder not found, skipping...")
            return

        print("Checking extra packages")

        # Load and clone repositories from extra_packages.repos file
        extra_repos = self.get_repositories_from_yaml("{}/extra_packages.repos".format(self.packages_folder))
        for repo_name in extra_repos:
            repo_values = extra_repos[repo_name]
            version = repo_values['version'] if 'version' in repo_values else None
            Repository(repo_name, repo_values['url'], self.distro, version).clone(self.mcu_src_folder)
            print("\t - Downloaded {}".format(repo_name))

        extra_folders = os.listdir(self.packages_folder)
        if 'extra_packages.repos' in extra_folders:
            extra_folders.remove('extra_packages.repos')

        for folder in extra_folders:
            print("\t - Adding {}".format(folder))

        shutil.copytree(self.packages_folder, self.mcu_src_folder, ignore=shutil.ignore_patterns('extra_packages.repos'), dirs_exist_ok=True)

    def get_repositories_from_yaml(self, yaml_file):
        repos = {}
        try:
            with open(yaml_file, 'r') as repos_file:
                root = yaml.safe_load(repos_file)
                repositories = root['repositories']

            if repositories:
                for path in repositories:
                    repo = {}
                    attributes = repositories[path]
                    try:
                        repo['type'] = attributes['type']
                        repo['url'] = attributes['url']
                        if 'version' in attributes:
                            repo['version'] = attributes['version']
                    except KeyError as e:
                        continue
                    repos[path] = repo
        except (yaml.YAMLError, KeyError, TypeError) as e:
            print("Error on {}: {}".format(yaml_file, e))
        finally:
            return repos
        
    def build_mcu_environment(self, meta_file, toolchain_file, user_meta=""):
        print("Building micro-ROS library")

        install_prefix = f"{self.mcu_folder}/install"
        common_meta_path = self.library_folder + "/metas/common.meta"

        if sys.platform.startswith("win"):
            dev_setup_bat = f"{self.dev_folder}/install/setup.bat"
            bat_env, err = load_env_from_bat(dev_setup_bat)
            if not bat_env:
                raise RuntimeError(f"Impossible de charger l'env via setup.bat ({dev_setup_bat}): {err}")

            merged_env = dict(self.env) if self.env else {}
            merged_env.update(bat_env)  # <-- AJOUT CRITIQUE (sinon ament_cmake est invisible)

            dev_prefix = os.path.normpath(os.path.join(self.dev_folder.replace("/", "\\"), "install"))
            for var in ("AMENT_PREFIX_PATH", "CMAKE_PREFIX_PATH", "COLCON_PREFIX_PATH"):
                cur = merged_env.get(var, "")
                if dev_prefix not in cur.split(";"):
                    merged_env[var] = dev_prefix + (";" + cur if cur else "")

            merged_env.setdefault("PYTHONUTF8", "1")
            merged_env.setdefault("PYTHONIOENCODING", "utf-8")
            merged_env["CMAKE_BUILD_PARALLEL_LEVEL"] = "1"

            # PATHEXT
            _ensure_pathext(merged_env)

            # wrappers ament_* (OK, et on le fait APRES update(bat_env) pour ne pas se faire écraser)
            wrappers_dir = os.path.join(self.build_folder.replace("/", "\\"), "win_ament_wrappers")
            _write_ament_wrappers(wrappers_dir)
            merged_env["PATH"] = wrappers_dir + os.pathsep + merged_env.get("PATH", "")

            # Éviter colcon_powershell : inutile ici et source du crash actuel
            block = merged_env.get("COLCON_EXTENSION_BLOCKLIST", "")
            entry = "colcon_core.shell.powershell"
            items = [x for x in block.split(os.pathsep) if x]
            if entry not in items:
                items.append(entry)
            merged_env["COLCON_EXTENSION_BLOCKLIST"] = os.pathsep.join(items)

            # Déduplication Windows (PATH/Path, PATHEXT/Pathext, etc.)
            merged_env = _normalize_windows_env(merged_env)

            # python.exe PlatformIO penv
            penv_scripts = os.path.dirname(self.python_env.replace("/", "\\"))
            python_exe_win = os.path.join(penv_scripts, "python.exe")
            python_exe_cmake = python_exe_win.replace("\\", "/")
            toolchain_cmake = toolchain_file.replace("\\", "/")

            # exécuter colcon en "process Windows" (pas via bash)
            args = [
                python_exe_win, "-m", "colcon", "build",
                "--executor", "sequential",
                "--event-handlers",
                "console_direct+",
                "desktop_notification-",
                "status-",
                "terminal_title-",
                "--merge-install",
                "--packages-ignore-regex", ".*_cpp",
                "--metas", common_meta_path, meta_file
            ]
            if user_meta:
                args += [user_meta]

            args += [
                "--cmake-args",
                "-G", "Ninja",
                f"-DCMAKE_TOOLCHAIN_FILE:FILEPATH={toolchain_cmake}",
                f"-DPython3_EXECUTABLE:FILEPATH={python_exe_cmake}",
                "-DCMAKE_BUILD_TYPE=Release",
                "-DBUILD_TESTING=OFF",

                # clé pour micro-CDR (sinon superbuild => pas d'install => microxrcedds_client casse)
                "-DUCDR_SUPERBUILD=OFF",
                "-DUCDR_ISOLATED_INSTALL=OFF",

                # éviter les \U etc
                f"-DPython3_EXECUTABLE={python_exe_cmake}",
            ]
            comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
            chk = run_proc([comspec, "/d", "/s", "/c", "where ament_append_value"], env=merged_env)
            print("WHERE ament_append_value rc=", chk.returncode)
            print(chk.stdout or "")
            print(chk.stderr or "")

            block = merged_env.get("COLCON_EXTENSION_BLOCKLIST", "")
            items = [x for x in block.split(os.pathsep) if x]

            for entry in (
                "colcon_notification.event_handler.desktop_notification",
                "colcon_notification.event_handler.status",
                "colcon_notification.event_handler.terminal_title",
            ):
                if entry not in items:
                    items.append(entry)

            merged_env["COLCON_EXTENSION_BLOCKLIST"] = os.pathsep.join(items)

            result = run_proc(args, cwd=self.mcu_folder.replace("/", "\\"), env=merged_env)

        else:
            # (ta branche non-Windows inchangée)
            colcon_command = '. "{}" && colcon build --event-handlers console_direct+ desktop_notification- --merge-install --packages-ignore-regex=.*_cpp --metas "{}" "{}" {} --cmake-args -G Ninja -DCMAKE_INSTALL_PREFIX="{}" -DUCDR_SUPERBUILD=OFF -DUCDR_ISOLATED_INSTALL=OFF -DCMAKE_POSITION_INDEPENDENT_CODE:BOOL=OFF -DTHIRDPARTY=ON -DBUILD_SHARED_LIBS=OFF -DBUILD_TESTING=OFF -DCMAKE_BUILD_TYPE=Release -DCMAKE_TOOLCHAIN_FILE="{}" -DPython3_EXECUTABLE=`which python`'.format(
                self.python_env, common_meta_path, meta_file, user_meta, install_prefix, toolchain_file
            )
            command = f'cd "{self.mcu_folder}" && . "{self.dev_folder}/install/setup.sh" && {colcon_command}'
            result = run_cmd(command, env=self.env)
        
        if result.returncode != 0:
            # robustesse encodage
            stderr = result.stderr
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            print(f"Build mcu micro-ROS environment failed:\n{stderr}")
            sys.exit(1)

    def package_mcu_library(self):
        """
        Construit libmicroros.a en fusionnant toutes les .a dans build/mcu/install/lib.
        Windows:
        - extraction via arm-none-eabi-ar
        - renomme les objets en noms très courts (o0000001.obj/ .o) pour éviter WinError 206
        - ar en chunks
        """
        aux_root = Path(os.environ.get("MICROROS_AUX_DIR", str(Path(self.build_folder) / "aux"))).resolve()
        extract_dir = aux_root / "_extract"
        out_lib = aux_root / "libmicroros.a"

        # Clean
        shutil.rmtree(aux_root, ignore_errors=True)
        aux_root.mkdir(parents=True, exist_ok=True)
        extract_dir.mkdir(parents=True, exist_ok=True)
        Path(self.library_path).mkdir(parents=True, exist_ok=True)

        install_lib_dir = Path(self.mcu_folder) / "install" / "lib"
        if not install_lib_dir.is_dir():
            raise RuntimeError(f"Dossier introuvable: {install_lib_dir}")

        archives = list(install_lib_dir.rglob("*.a"))
        if not archives:
            raise RuntimeError(f"Aucune archive .a trouvée dans {install_lib_dir}")

        if sys.platform.startswith("win"):
            ar_exe, ranlib_exe = _find_binutils()

            # 1) extraire toutes les archives et copier les objets avec des noms courts
            counter = 0
            for a in archives:
                # purge extract_dir
                for p in extract_dir.iterdir():
                    try:
                        if p.is_file():
                            p.unlink()
                        else:
                            shutil.rmtree(p, ignore_errors=True)
                    except OSError:
                        pass

                r = subprocess.run([ar_exe, "x", str(a)], cwd=str(extract_dir), capture_output=True)
                if r.returncode != 0:
                    raise RuntimeError(
                        f"ar x failed for {a}\n{r.stderr.decode('utf-8', errors='replace')}"
                    )

                for obj in extract_dir.iterdir():
                    if obj.suffix.lower() not in (".o", ".obj"):
                        continue
                    counter += 1
                    new_name = f"o{counter:07d}{obj.suffix.lower()}"
                    obj.replace(aux_root / new_name)

            # 2) créer libmicroros.a (chunking)
            if out_lib.exists():
                out_lib.unlink()

            objs = sorted([p.name for p in aux_root.glob("o*.o")] + [p.name for p in aux_root.glob("o*.obj")])
            if not objs:
                raise RuntimeError("Aucun objet (*.o/*.obj) trouvé pour construire libmicroros.a")

            CHUNK = int(os.environ.get("MICROROS_AR_CHUNK", "256"))
            first = True
            for chunk in _chunked(objs, CHUNK):
                cmd = [ar_exe, ("rc" if first else "r"), out_lib.name, *chunk]
                first = False
                rr = subprocess.run(cmd, cwd=str(aux_root), capture_output=True)
                if rr.returncode != 0:
                    raise RuntimeError(rr.stderr.decode("utf-8", errors="replace"))

            rr2 = subprocess.run([ranlib_exe, out_lib.name], cwd=str(aux_root), capture_output=True)
            if rr2.returncode != 0:
                raise RuntimeError(rr2.stderr.decode("utf-8", errors="replace"))

        else:
            # Non-Windows: ton ancien chemin (bash) peut rester si tu veux
            binutils_path = self.resolve_binutils_path()
            cmd = f'{binutils_path}ar rc "{out_lib}" $(ls *.o *.obj 2> /dev/null); {binutils_path}ranlib "{out_lib}"'
            # Ici tu peux garder ton run_cmd existant si tu l’as déjà
            result = run_cmd(cmd)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))

        # Déplacer vers libmicroros/libmicroros.a
        target = Path(self.library)
        if target.exists():
            target.unlink()
        out_lib.replace(target)

    def resolve_binutils_path(self):
        # macOS (inchangé)
        if sys.platform == "darwin":
            homebrew_binutils_path = "/opt/homebrew/opt/binutils/bin/"
            if os.path.exists(homebrew_binutils_path):
                return homebrew_binutils_path
            print("ERROR: GNU binutils not found. ({}) Please install binutils with homebrew: brew install binutils"
                .format(homebrew_binutils_path))
            sys.exit(1)

        # Windows: trouver arm-none-eabi-ar / ranlib
        if sys.platform.startswith("win"):
            import shutil

            ar = shutil.which("arm-none-eabi-ar")
            ranlib = shutil.which("arm-none-eabi-ranlib")

            if not ar or not ranlib:
                # fallback: chercher dans le package PlatformIO toolchain-rp2040-earlephilhower
                pio_pkgs = os.path.join(os.path.expanduser("~"), ".platformio", "packages")
                cand = os.path.join(pio_pkgs, "toolchain-rp2040-earlephilhower", "bin")
                ar2 = os.path.join(cand, "arm-none-eabi-ar.exe")
                ranlib2 = os.path.join(cand, "arm-none-eabi-ranlib.exe")
                if os.path.isfile(ar2) and os.path.isfile(ranlib2):
                    # on renvoie le dossier bin/ (avec slash final pour concat)
                    return cand + os.sep

                raise RuntimeError(
                    "Binutils introuvables (arm-none-eabi-ar / arm-none-eabi-ranlib). "
                    "Vérifie que le toolchain RP2040 PlatformIO est installé."
                )

            # on renvoie le dossier bin/ (avec slash final pour concat)
            return os.path.dirname(ar) + os.sep

        # Linux: vide => on s'attend à avoir ar/ranlib dans PATH
        return ""
