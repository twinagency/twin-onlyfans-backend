# Changes needed in service/onlyfans_runtime/text_delivery.py (not edited by B3)

1. `TextAuthority.dispatch` accepts transports by exact type. Add
   `from fans_api_adapter.real_transport import FansApiRealTransport` next to the other lazy imports and add
   `FansApiRealTransport` to the set
   `{SyntheticTextTransport, FansApiSyntheticTransport, FansApiLocalHttpTransport, FansApiProtocolLabTransport}`.
   The B3 tests and live harness avoid the edit by monkeypatching `fans_api_adapter.protocol_lab.FansApiProtocolLabTransport`
   to `FansApiRealTransport` (dispatch re-imports that name per call). Remove that patch once registered.
2. The real transport runs under policy `transport_kind == 'synthetic'` (the engine-managed kind; the DB check
   constraint allows only synthetic/native and `dispatch` expects "synthetic" for this transport). A distinct
   `fansapi` kind needs a migration plus the ~25 `transport_kind != 'synthetic'` gates in conversation_engine/,
   fans_api_adapter/intake.py and text_delivery.py (`expire_dispatches/repair/materialize(synthetic_engine_only=True)`).
3. Ids in `Dispatch.media_ids` / `_validate_grant` are ints; real vault ids fit (BIGINT). Opaque `ofapi_media_*` upload ids would not.
