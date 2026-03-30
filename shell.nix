{
  pkgs ? import <nixpkgs> { },
}:

pkgs.mkShell {
  buildInputs = [
    (pkgs.python3.withPackages (
      ps: with ps; [
        numpy
        pandas
        matplotlib
        seaborn
        scipy
        statsmodels
        jupyter
      ]
    ))
  ];
}
