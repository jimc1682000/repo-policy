# repo-policy

## Purpose

This repository defines reusable GitHub repository policies and automation. Policy definitions live in `policies/`, repository inventories in the YAML files at the root, and automation in `scripts/`.

## Verify

Run `python3 -m unittest discover` after changes to scripts or policies; inspect the affected workflow and policy together for consistent behavior.

## Boundaries

Keep policy and consumer configuration in their existing YAML files. Follow the operational guidance in `docs/` for personal account control-plane changes.
