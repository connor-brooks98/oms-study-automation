# Uploads, notebook delivery, and mistaken files

Updated October 8, 2026. This describes the current implementation; installing the new release is a separate operator step.

## Upload completion

The Hub completes material processing when the lecture files finish their intake steps. Quiz creation and delivery to another application are separate actions. A lecture can be complete in the Hub while a notebook delivery is still pending.

The retired NotebookLM connector is removed. On startup, only abandoned optional sync records that were never attempted, have no remote source or active operation/lease, and have the exact old optional-sync diagnostic are retired as **skipped**. Their history remains available. Other unfinished or failed work still needs attention; no skipped item is described as uploaded.

## NotebookLM through ExtendLM

Finish ExtendLM setup in Settings and explicitly select the account and notebook for the course/exam. Once saved, each lecture has **Send to [notebook]**. **Change destination** is available on that card. Keep the selected ExtendLM browser running.

The card sends the available lecture PDF and cleaned transcript. Its receipts belong to that lecture and retain the original account, notebook and file revisions. Accepted files are not sent again when only indexing remains pending. Automatic indexing checks are bounded; refresh readiness later when needed. A partial delivery stays partial until all selected files are ready.

Changing accounts or losing access to the saved notebook requires choosing a destination again. The Hub never guesses a notebook from its title.

## Clinical Notebook

In Clinical Notebook, open **Settings → Connected Apps**, create a connection for the Hub, and grant the destination notebooks plus **List notebooks** and **Add sources**. Those permissions are sufficient for sending. Other applications that need to read or export existing sources can be granted those capabilities separately. Copy the new token once.

In Hub **Settings → Clinical Notebook**, enter its reachable API address and token, then select a destination for the course/exam. On a lecture, select the PDF and/or cleaned transcript and use **Send to Clinical Notebook**. The Hub validates and pins the selected revisions before sending. Each file has a separate receipt, and retries reuse accepted files.

A remote Hub needs an address reachable from the Hub computer. Use an external-only HTTPS gateway or an authenticated private tunnel with a loopback endpoint on that computer. Copying the Mac's loopback address directly into a Hub running elsewhere does not create a connection. Clinical Notebook's generic connection guide documents both arrangements and endpoint discovery.

Keep Clinical Notebook open for delivery. If it is unavailable, the prepared files stay pending; open the app and retry. **Refresh readiness** checks accepted files without uploading them again. Readable source text does not imply semantic indexing or quiz generation. Changing an already imported document produces a conflict for owner review, rather than an automatic replacement or duplicate.

See the [consumer contract and setup notes](../contracts/clinical-notebook-sources-v1.md). The Clinical Notebook repository owns the full API contract, independent-client example and receiver setup guide.

## Import practice questions

The Quiz Builder still accepts practice questions from files, pasted text and supported web URLs, including the URL workflow used for external question sources such as Dr Wang's blog. Missing answers remain private for manual completion and review before publication. GPT lecture quiz creation remains available on lecture pages. NotebookLM-based quiz generation is retired.

## Delete an unmatched upload

Open **Quarantine**, find the mistakenly uploaded item and choose **Delete upload**. Review the filename in the confirmation. This action applies to unmatched staged uploads with no lecture, ingestion job or material revision attached; it does not remove other files from a shared batch.

The Hub records removal before cleaning up the staged file. If file cleanup is temporarily unavailable, the item shows **Retry cleanup** instead of pretending the file is gone. For materials already assigned to a lecture, use the lecture's existing material-removal controls.
