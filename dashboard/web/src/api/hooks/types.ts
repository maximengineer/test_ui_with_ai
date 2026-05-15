import type { components } from '../schema.gen'

export type RunRow = components['schemas']['RunRow']
export type RunListOut = components['schemas']['RunListOut']
export type SiteOut = components['schemas']['SiteOut']
export type DatesOut = components['schemas']['DatesOut']
export type HealthOut = components['schemas']['HealthOut']
export type SyncOut = components['schemas']['SyncOut']
export type RunSpawnedOut = components['schemas']['RunSpawnedOut']
export type ReportSummaryOut = components['schemas']['ReportSummaryOut']
export type ReportUrlsOut = components['schemas']['ReportUrlsOut']
export type ReportUrlSummary = components['schemas']['ReportUrlSummary']
export type ReportUrlDetail = components['schemas']['ReportUrlDetail']

export type ReportResultType = ReportUrlSummary['result_type']
export type RunKind = RunRow['kind']
export type RunStatus = RunRow['status']
