"""Where optional features register their loaders and plain startup templates (G0 ships none).

Imported during foundation initialization in the parent and child. Do not import feature modules here: their loaders run on workers.
"""
