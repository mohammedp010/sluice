/// <reference types="vite/client" />

declare module "virtual:git-log" {
  export interface Commit {
    hash: string;
    date: string;
    subject: string;
    body: string;
    files: number;
    added: number;
    removed: number;
  }
  const commits: Commit[];
  export default commits;
}
