# Run recipes in PowerShell on Windows (just defaults to `sh`)
set windows-shell := ["pwsh", "-NoProfile", "-Command"]

# List available recipes
default:
    @just --list
