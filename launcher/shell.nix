# Launcher environment: `nix-shell launcher/shell.nix --run 'python3 launcher/sacredfire_launcher.py'`
{ pkgs ? import <nixpkgs> {} }:
pkgs.mkShell {
  packages = with pkgs; [
    (python3.withPackages (ps: [ ps.pygobject3 ]))
    gtk4 libadwaita gobject-introspection
  ];
  # GTK's settings schemas, as the packaged launcher has them (packaging/default.nix).
  shellHook = ''
    export XDG_DATA_DIRS=${pkgs.gtk4}/share/gsettings-schemas/${pkgs.gtk4.name}:${pkgs.gsettings-desktop-schemas}/share/gsettings-schemas/${pkgs.gsettings-desktop-schemas.name}''${XDG_DATA_DIRS:+:$XDG_DATA_DIRS}
  '';
}
