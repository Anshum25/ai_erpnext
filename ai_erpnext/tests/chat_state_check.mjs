// Node check of public/js/ai_chat_state.js: the widget's Cancel control and response ordering (run by tests/test_chat_cancel.py).
import assert from "node:assert/strict";
import {
  cancelControl,
  insertBeforeId,
  isStale,
  questionChoices,
} from "../public/js/ai_chat_state.js";

// No workflow, no Cancel control.
assert.equal(cancelControl(null), null);
assert.equal(cancelControl(undefined), null);
assert.equal(cancelControl({ label: "Creating Sales Invoice (Draft)" }), null); // no id: nothing to bind it to

// A workflow shows its label and a Cancel that sends the workflow's own id (a Cancel for an ended workflow is ignored).
const control = cancelControl({
  id: "wf1",
  kind: "form",
  label: "Creating Sales Invoice (Draft)",
});
assert.equal(control.label, "Cancel");
assert.equal(control.text, "Creating Sales Invoice (Draft)");
assert.match(control.ariaLabel, /^Cancel: Creating Sales Invoice/);
assert.deepEqual(control.action, { command: "cancel", workflow_id: "wf1" });
assert.notEqual(control.action.command, "submit"); // never a document action

// A question's Cancel comes after its answers, in the same choice area, styled as secondary.
const q = {
  id: "q1",
  options: [
    { label: "vishnu", value: "vishnu" },
    { label: "Show more customers", value: "__more__", style: "secondary" },
    {
      label: "Create new customer “Rahul”",
      value: "__create__",
      style: "secondary",
    },
  ],
  cancel: { label: "Cancel", value: "__cancel_all__" },
};
const choices = questionChoices(q);
assert.deepEqual(
  choices.map((c) => c.value),
  ["vishnu", "__more__", "__create__", "__cancel_all__"]
);
assert.deepEqual(choices.at(-1), {
  label: "Cancel",
  value: "__cancel_all__",
  secondary: true,
  cancel: true,
});
assert.equal(choices.filter((c) => c.cancel).length, 1);
assert.equal(choices[2].cancel, false); // "Create new" is not the Cancel

// A text-only question still gets its Cancel; a question without one gets none; a Cancel already among the options isn't doubled.
assert.deepEqual(
  questionChoices({
    id: "q2",
    options: [],
    cancel: { label: "Cancel", value: "__cancel_all__" },
  }).length,
  1
);
assert.deepEqual(
  questionChoices({ id: "q3", options: [{ label: "Yes", value: "y" }] }).length,
  1
);
assert.equal(
  questionChoices({
    id: "q4",
    options: [{ label: "Cancel", value: "__cancel__" }],
    cancel: { label: "Cancel", value: "__cancel__" },
  }).length,
  1
);
assert.deepEqual(questionChoices(null), []);

// Ordering: state only from the newest response; late messages go in their place.
assert.equal(isStale(3, 4), true);
assert.equal(isStale(4, 4), false);
assert.equal(isStale(5, 4), false);
assert.equal(insertBeforeId([1, 2, 5, 6], 3), 5);
assert.equal(insertBeforeId([1, 2], 3), null);
assert.equal(insertBeforeId([], 1), null);

console.log("ok");
