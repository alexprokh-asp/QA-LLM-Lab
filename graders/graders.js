/**
 * Shared JavaScript graders for PolicyBot promptfoo tests.
 * Usage in YAML:
 *   - type: javascript
 *     value: file://graders.js:isValidPolicyEvaluationJson
 *   - type: javascript
 *     value: file://graders.js:hasPolicy
 *     config:
 *       expectedPolicy: "Annual Leave Policy"
 *   - type: javascript
 *     value: file://graders.js:hasRequiresApproval
 *     config:
 *       expectedRequiresApproval: false
 */

function safeParseJson(output) {
  try {
    return JSON.parse(output);
  } catch {
    return null;
  }
}

/**
 * Checks that the output is a valid Policy Evaluation JSON object
 * with exactly the three required fields and correct types.
 */
function isValidPolicyEvaluationJson(output) {
  const o = safeParseJson(output);
  return (
    o !== null &&
    typeof o === 'object' &&
    !Array.isArray(o) &&
    Object.keys(o).length === 3 &&
    typeof o.answer === 'string' &&
    typeof o.policy === 'string' &&
    typeof o.requires_approval === 'boolean'
  );
}

/**
 * Checks that the `policy` field equals the expected value from config.
 * Requires: config.expectedPolicy (string)
 */
function hasPolicy(output, context) {
  const o = safeParseJson(output);
  if (!o) return false;
  return o.policy === context.config.expectedPolicy;
}

/**
 * Checks that the `requires_approval` field equals the expected boolean.
 * Requires: config.expectedRequiresApproval (boolean)
 */
function hasRequiresApproval(output, context) {
  const o = safeParseJson(output);
  if (!o) return false;
  return o.requires_approval === context.config.expectedRequiresApproval;
}

module.exports = {
  isValidPolicyEvaluationJson,
  hasPolicy,
  hasRequiresApproval,
};
