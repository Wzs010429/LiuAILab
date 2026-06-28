# Liu AI Lab Website

This repository contains the Jekyll source for the Liu AI Lab website.

## Local Development

```sh
jekyll serve --livereload
```

On this Windows machine, Ruby is installed at `C:\Ruby32-x64`. If a terminal has not picked up the new `PATH` yet, run:

```powershell
C:\Ruby32-x64\bin\jekyll.bat serve --livereload
```

The production custom domain is configured in `CNAME`.

## Image WebP Automation

This repo includes hooks that convert files under `images/` from `.png`, `.jpg`,
and `.jpeg` to `.webp`, delete the originals, and update text references before
commits/pushes. The script also checks known local image references and fails if
any converted photo path would point to a missing file.

Install the hooks once per clone:

```powershell
.\scripts\install_git_hooks.ps1
python -m pip install Pillow
```

Manual dry run:

```powershell
python .\scripts\convert_images_to_webp.py --check-deps
python .\scripts\convert_images_to_webp.py --dry-run
```
