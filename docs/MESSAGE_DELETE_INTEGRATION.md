# Delete a message block — frontend integration

Deleting a chat message now deletes the **whole block** it produced on the
backend — the message, the generated version, its processing job, and its
image/mask file(s) in storage. Frontend only needs to make one call and then
remove the block from the UI.

This is **one shared endpoint used by all three chats**:

| Step | Chat | What gets deleted with the message |
|---|---|---|
| Step 1 | 2D floor-plan chat | The `FloorPlanVersion`, its job, its image/mask |
| Step 2 | 3D rendering chat | The `ThreeDVersion`, its job, its image/mask |
| Step 3 | BOQ chat | The `BOQVersion` and its job |

Same URL, same method, same response shape for all three — no per-step
branching needed on the frontend.

## Endpoint

```
DELETE /api/v1/projects/{project_id}/conversations/messages/{message_id}/
```

`message_id` is the id of the **user message** shown in the block you want to
remove (e.g. the "Update Sketch" message), not the generated image's asset id.

## Request

```http
DELETE /api/v1/projects/{project_id}/conversations/messages/{message_id}/
Authorization: Bearer <token>
```

No body. Works identically whether the message came from the Step 1
floor-plan chat, the Step 2 3D-rendering chat, or the Step 3 BOQ chat.

## Success response — `200 OK`

```json
{
  "success": true,
  "message": "Message block deleted successfully",
  "deleted_message_id": "123"
}
```

On success, remove the entire block (user message + its generated
image/render/BOQ output) from the UI. **Do not call a separate delete for the
image** — the backend already removed the version, its job, and its file(s).

## Error responses

| Status | When |
|---|---|
| `404 Not Found` | Message doesn't exist, or doesn't belong to a project you own |
| `401 Unauthorized` | Missing/invalid auth token |

```json
{
  "detail": "Not found."
}
```

## ⚠️ Breaking change if you already call this endpoint

This endpoint existed before but only deleted the message row and returned
`204 No Content` with no body, leaving the generated image orphaned. It now
returns `200 OK` with the JSON body above instead. If your existing code
checks for `204` or expects an empty body, update it to check
`response.data.success` on `200` instead.

## Example (fetch)

```js
async function deleteMessageBlock(projectId, messageId, token) {
  const res = await fetch(
    `/api/v1/projects/${projectId}/conversations/messages/${messageId}/`,
    {
      method: 'DELETE',
      headers: { Authorization: `Bearer ${token}` },
    }
  );

  if (!res.ok) {
    throw new Error(`Delete failed: ${res.status}`);
  }

  const data = await res.json();
  if (data.success) {
    removeBlockFromUI(data.deleted_message_id);
  }
}
```

## Good to know

- Deleting the "current" version of a project (the one selected in Step 2/3)
  clears that selection — the project will show no selected render until the
  user picks another one from history.
- If a later message was generated *from* the one you deleted (e.g. an edit
  built on top of it), that later message and its own image are **not**
  deleted — only the exact block you targeted is removed. The later block's
  "based on" link just becomes unset; it still has its own valid image.
