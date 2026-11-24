## What changes

<!-- What it does and why. The title must be a conventional commit: it becomes the changelog entry. -->

## Checklist

- [ ] Behaviour changes start in `spec/SPEC.md` and in the generator, `tools/gen_vectors.py`; vectors and generated files are not edited by hand
- [ ] Both ports are updated, or the pull request says why only one is
- [ ] A fixed bug has a vector that fails without the fix, and a mutant if it fits
- [ ] Docs and the conformance matrix say what is true after this change
