# ApplyPilot Playwright shell — provides system libraries needed by Playwright's
# bundled Chromium on NixOS.
#
# Usage:
#   nix-shell shell.nix --run "bash run-ap.sh run enrich"
#   nix-shell shell.nix --run "python3 -m applypilot run enrich"
#
# Or activate interactively:
#   nix-shell shell.nix
#   bash run-ap.sh run enrich

{ pkgs ? import <nixpkgs> {} }:

let
  # Libraries needed by Playwright's bundled Chromium headless shell
  playwright-libs = with pkgs; [
    stdenv.cc.cc.lib  # for numpy / libstdc++
    glib
    gobject-introspection
    nss
    nspr
    atk
    at-spi2-atk
    at-spi2-core
    dbus
    expat
    libxkbcommon
    libX11
    libXcomposite
    libXdamage
    libXext
    libXfixes
    libXrandr
    libxcb
    mesa # for libgbm
    systemd # for libudev
    pango
    cairo
    gdk-pixbuf
    gtk3
    cups
  ];

in
pkgs.mkShell {
  name = "applypilot-playwright";

  buildInputs = playwright-libs;

  shellHook = ''
    # Build LD_LIBRARY_PATH from all the library paths
    export LD_LIBRARY_PATH=""
    for pkg in ${builtins.toString playwright-libs}; do
      libdir="$pkg/lib"
      if [ -d "$libdir" ]; then
        export LD_LIBRARY_PATH="''${LD_LIBRARY_PATH:+''${LD_LIBRARY_PATH}:}$libdir"
      fi
    done
    echo "Playwright environment ready"
    echo "LD_LIBRARY_PATH includes $(echo $LD_LIBRARY_PATH | tr ':' '\n' | wc -l) library paths"
  '';
}
