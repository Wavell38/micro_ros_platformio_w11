# micro_ros_platformio_w11

A PlatformIO-oriented fork of `micro_ros_platformio` focused on **native Windows support**.

This fork is intended to make micro-ROS usable from PlatformIO on Windows without relying on WSL for the normal build flow.

It keeps the general `micro_ros_platformio` workflow, while adding the fixes needed for:

* native Windows package generation,
* proper include/library export to PlatformIO projects,
* easier consumption from a Git dependency,
* RP2040 / Arduino-Pico project integration.

## Scope

This fork is primarily focused on **Windows** and has been validated mainly with:

* **Windows native build flow**,
* **RP2040 / Raspberry Pi Pico** projects using **Arduino-Pico**,
* PlatformIO projects consuming the library directly from Git.

Other boards and platforms may still work, but this fork is documented and maintained first for the Windows workflow.

## What this fork changes

Compared to the upstream `micro_ros_platformio`, this fork is intended to:

* build micro-ROS dependencies directly on Windows,
* export generated include paths automatically to the user project,
* export the required library paths automatically to the user project,
* remain consumable through `lib_deps` with a Git URL,
* keep project-side workarounds limited to target-specific cases only.

## Installation in a PlatformIO project

Add the dependency through Git in `platformio.ini`.

### Recommended Windows configuration

On Windows, **use short PlatformIO directories**.

ROS 2 / micro-ROS generated paths can become very long, especially when the library is installed under `.pio/libdeps/...`. Using short directories avoids path-length issues during package generation.

Example:

```ini
[platformio]
workspace_dir = C:/.pio_mros
libdeps_dir = C:/.pio_libdeps

[env:pico]
platform = https://github.com/maxgerhardt/platform-raspberrypi.git
board = pico
framework = arduino
board_build.core = earlephilhower

board_microros_distro = jazzy
board_microros_transport = serial

lib_deps =
    https://github.com/Wavell38/micro_ros_platformio_w11.git

build_flags =
    -D USE_TINYUSB

extra_scripts =
    pre:scripts/pio_prebuild.py
    post:scripts/microros_rp2040_atomic_fix.py
```

## Important notes

### 1. Windows path length

If you build this library from a Git dependency on Windows, long paths can break some generated ROS 2 packages.

Symptoms can look like:

* `Filename longer than 260 characters`
* random failures in generated interface packages
* failures inside `.pio/libdeps/.../build/mcu/build/...`

The recommended fix is to use short values for:

* `workspace_dir`
* `libdeps_dir`

as shown above.

### 2. Project pre-build script

If your project uses `.pio` source files (for example RP2040 PIO state machines), keep a small project-level pre-build script for `pioasm` generation.

That script should only handle the project-specific `.pio` headers.

The fork itself now exports the micro-ROS include and library paths automatically, so the project script should **not** hardcode any path to the fork.

### 3. RP2040 / Arduino-Pico note

For RP2040 / Pico projects using Arduino-Pico, a duplicate atomic symbol conflict may appear at link time depending on the toolchain / Pico SDK combination.

This fork does **not** patch `libmicroros.a` globally.

Instead, the recommended approach is:

* keep the fork generic,
* use an **optional project-level post-build script** for RP2040,
* create a **local patched copy** of `libmicroros.a` for that project only,
* redirect the linker for that environment only.

This keeps the fork reusable for other boards and avoids mutating the shared archive globally.

## Supported configuration keys

This fork follows the usual micro-ROS PlatformIO project options:

```ini
board_microros_distro = jazzy
board_microros_transport = serial
```

The exact set of working combinations depends on the target board and framework.

## Typical project layout

A practical Windows/RP2040 project usually looks like this:

```text
project/
├─ platformio.ini
├─ scripts/
│  ├─ pio_prebuild.py
│  └─ microros_rp2040_atomic_fix.py
└─ src/
   ├─ main.cpp
   ├─ step_gen.pio
   └─ step_count.pio
```

Where:

* `pio_prebuild.py` generates `.pio.h` headers with `pioasm`,
* `microros_rp2040_atomic_fix.py` is optional and only needed for the RP2040 atomic-symbol conflict case.

## Current status

This fork is intended to provide a practical Windows-native workflow for micro-ROS with PlatformIO.

It has been adapted around real project usage and tested primarily on Windows with RP2040/Pico-based builds.

## Upstream reference

This project is based on the original `micro_ros_platformio` work.

If you need the upstream project or broader original documentation, refer to:

* `micro-ROS/micro_ros_platformio`

## Summary

Use this fork if you want:

* micro-ROS from PlatformIO,
* native Windows build flow,
* Git-based dependency usage,
* automatic export of micro-ROS include/library paths,
* a practical base for Pico / RP2040 projects.

If your target is RP2040 and you hit a final link conflict, keep that fix at the **project level**, not as a global mutation of the shared library.
