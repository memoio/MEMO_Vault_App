type Project = { project_id: string; parent_project_id: string | null };
type Count = { project_id: string; visible_valid_count: number; syncing_count: number };

export function projectSubtree<T extends Project>(projects: T[], rootId: string): T[] {
  const included = new Set([rootId]);
  let previousSize = 0;
  while (previousSize !== included.size) {
    previousSize = included.size;
    for (const project of projects) {
      if (project.parent_project_id && included.has(project.parent_project_id)) {
        included.add(project.project_id);
      }
    }
  }
  return projects.filter(project => included.has(project.project_id));
}

export function subtreeCount(projects: Project[], counts: Count[], rootId: string) {
  const ids = new Set(projectSubtree(projects, rootId).map(project => project.project_id));
  return counts.reduce((total, count) => {
    if (ids.has(count.project_id)) {
      total.visible_valid_count += count.visible_valid_count;
      total.syncing_count += count.syncing_count;
    }
    return total;
  }, { visible_valid_count: 0, syncing_count: 0 });
}
