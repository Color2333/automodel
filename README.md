# Meshy AutoGLB LogoSlot

Blender add-on for Logo / Slot GLB review, repair workflow tracking, and final export management.

This branch contains the LogoSlot-focused workflow plugin (`Meshy-AutoGLB`, version 4.5.8). It is intended for operators who review generated GLB assets, mark Logo / Slot status, repair fixable models, and export final categorized results.

## Branch

- `logoslot`: Logo / Slot repair and export workflow.
- `main`: AutoModel workflow plugin.

## What It Does

- Imports source GLB models from a selected directory.
- Tracks model status such as fixable, fixed, no-logo, and hard-fix.
- Exports final GLB files into operator-specific folders.
- Generates CSV reports for final classification and missing export checks.
- Filters LogoSlot helper objects during export while allowing repaired mesh objects to export normally.

## Notes

- `LogoSlot_Boolean_Fixer` is not included in this repository yet.
- Zip archives and Python cache files are intentionally excluded.
- Install this branch as a Blender add-on from the branch source or package it into a zip before installation.

