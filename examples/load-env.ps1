# Load a .env file into this PowerShell session, then run lombo-shrink as usual.
#   . .\examples\load-env.ps1            (reads .env in the current folder)
#   . .\examples\load-env.ps1 C:\path\to\other.env
# lombo-shrink does not read .env itself. Lines look like NAME=value; blank lines and # comments are skipped.
param([string]$Path = ".env")
Get-Content -LiteralPath $Path | ForEach-Object {
  $line = $_.Trim()
  if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
    $name, $value = $line.Split("=", 2)
    Set-Item -Path "Env:$($name.Trim())" -Value $value.Trim().Trim("'", '"')
  }
}
