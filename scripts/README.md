# Auxiliary scripts

- `bundle_prediction_template.py`: rebuild the packaged experiment template from a reviewed source directory.
- `setup_wiki_embedding.py`: download the pinned local E5 encoder and tokenizer; does not install dependencies.
- `wiki_embedding_worker.py`: compute text embeddings in the configured Python environment. Called by `batteryscientist/tools/wiki/hybrid.py`; required when local semantic search is enabled.
- `land-mcp/`: Windows LAND MCP server and SSH bridge deployment source snapshots.

Normal research assignments use the agent entry points in `pyproject.toml`.
