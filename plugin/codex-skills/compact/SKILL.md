---
name: compact
description: Trim this session on the next model request. The earlier turns stay in the local store.
---

Tell the user the next request will trim. The hook writes the force flag when this session's prompt contains the marker below. Include the marker in your reply to the user only if the hook did not run; otherwise one short acknowledgement is enough.

<!-- openmem:smart-compact -->
