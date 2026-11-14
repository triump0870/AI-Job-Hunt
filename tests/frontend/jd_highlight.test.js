import { readFileSync } from 'node:fs';
import { createContext, runInContext } from 'node:vm';
import { describe, expect, it } from 'vitest';

// The description highlights matched and missing skills as whole words, as the backend grounds them: a gap named
// "Rust" must not light up inside "Zero Trust".
const src = readFileSync('app/static/app.js', 'utf8');
const pick = name => src.match(new RegExp(`^(?:const ${name} = .*|function ${name}\\([\\s\\S]*?\\n\\})$`, 'm'))[0];
const highlight = runInContext(`${pick('esc')}\n${pick('highlight')}\nhighlight`, createContext({}));
const marks = html => [...html.matchAll(/<mark class="([\w-]+)">(.*?)<\/mark>/g)].map(m => [m[1], m[2]]);

describe('JD highlighting', () => {
  it('marks a skill only as a whole word', () => {
    const html = highlight('The Zero Trust Exchange: trust through outcomes; strong Go, Rust, and Python.', ['Python'], ['Rust']);
    expect(marks(html)).toEqual([['m-gap', 'Rust'], ['m-good', 'Python']]);
    expect(html).toContain('Zero Trust Exchange');
  });

  it('keeps symbols that are part of a skill name', () => {
    const html = highlight('Modern C++ and C#; not C++11 here. Also .NET Core & Q&A.', ['C++', '.NET'], []);
    expect(marks(html)).toEqual([['m-good', 'C++'], ['m-good', '.NET']]);
  });
});
