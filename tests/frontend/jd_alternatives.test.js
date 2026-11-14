import { readFileSync } from 'node:fs';
import { createContext, runInContext } from 'node:vm';
import { describe, expect, it } from 'vitest';

// "One of Python, Java, Kotlin ..." is one requirement: the panel shows it once, not as seven Required rows.
const context = createContext({ esc: s => String(s), attr: s => String(s), pill: (label, tone) => `[${label}]`, console });
runInContext(readFileSync('app/static/job_data.js', 'utf8'), context);
const { jdRequirementGroups, jdRequirementItem } = runInContext('({ jdRequirementGroups, jdRequirementItem })', context);

const row = (id, value, group) => ({ id, value, level: 'REQUIRED', confidence: 0.95, alternative_group: group,
  source_text: 'one of the following: Python, Java or Kotlin' });

describe('JD requirements · alternatives', () => {
  it('shows a group of alternatives as one "Any one of" requirement in place of its first member', () => {
    const items = jdRequirementGroups([row('r1', 'Python', 'grp_001'), row('r2', 'Kafka', null),
      row('r3', 'Java', 'grp_001'), row('r4', 'Kotlin', 'grp_001')]);
    expect(items.map(item => item.alternatives || item.value)).toEqual([['Python', 'Java', 'Kotlin'], 'Kafka']);
    const html = jdRequirementItem(items[0]);
    expect(html).toContain('Any one of:');
    expect(html).toContain('data-jd-group="grp_001"');
  });

  it('leaves a lone member as an ordinary requirement', () => {
    const [item] = jdRequirementGroups([row('r1', 'Python', 'grp_009')]);
    expect(item.alternatives).toBeUndefined();
    expect(item.value).toBe('Python');
  });
});
