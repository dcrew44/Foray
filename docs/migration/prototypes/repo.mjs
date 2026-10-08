// Load the repo's installed packages by absolute path (read-only use of the repo).
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

export const REPO =
  '/Users/haydenbarolette/Projects/JobSearchAgent/.claude/worktrees/middleware-django-migration-389b1a';
const req = createRequire(REPO + '/package.json');

const load = async spec => import(pathToFileURL(req.resolve(spec)).href);

export const ai = await load('ai');
export const aiTest = await load('ai/test');
export const react = await load('@ai-sdk/react');
export const { z } = await load('zod');
export const versions = {
  ai: req('ai/package.json').version,
  '@ai-sdk/react': req('@ai-sdk/react/package.json').version,
  zod: req('zod/package.json').version,
};
