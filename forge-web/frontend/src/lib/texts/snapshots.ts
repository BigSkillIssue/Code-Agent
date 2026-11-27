// Texts for the snapshots after every run (W66), merged by i18n.ts.

export const snapshotsEn = {
  snapshotsTab: "Backups",
  snapshotsHint: "After every run Forge keeps the project's state, including files that are not committed yet.",
  snapshotsLocal: "No storage server yet – backups are kept on the Forge server",
  snapshotsNode: "Kept on a storage server",
  snapshotsNone: "No backups yet. The first one is made after the next run.",
  snapshotsOff: "Backups after every run are turned off on this server.",
  snapshotRestore: "Restore as branch",
  snapshotConfirm: "Restore the backup of {time} as a new branch? Your current branch and files stay as they are.",
  snapshotRestored: "Restored as branch {branch}",
  snapshotFailed: "failed",
  snapshotWhereNode: "storage server",
  snapshotWhereLocal: "Forge server",
};

export const snapshotsDe: typeof snapshotsEn = {
  snapshotsTab: "Sicherungen",
  snapshotsHint: "Nach jedem Lauf hält Forge den Stand des Projekts fest, auch Dateien, die noch nicht committet sind.",
  snapshotsLocal: "Noch kein Speicher-Server – Sicherungen liegen auf dem Forge-Server",
  snapshotsNode: "Liegen auf einem Speicher-Server",
  snapshotsNone: "Noch keine Sicherungen. Die erste entsteht nach dem nächsten Lauf.",
  snapshotsOff: "Sicherungen nach jedem Lauf sind auf diesem Server ausgeschaltet.",
  snapshotRestore: "Als Branch wiederherstellen",
  snapshotConfirm: "Die Sicherung von {time} als neuen Branch wiederherstellen? Dein aktueller Branch und deine Dateien bleiben, wie sie sind.",
  snapshotRestored: "Wiederhergestellt als Branch {branch}",
  snapshotFailed: "fehlgeschlagen",
  snapshotWhereNode: "Speicher-Server",
  snapshotWhereLocal: "Forge-Server",
};
