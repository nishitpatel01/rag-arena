import { useMemo } from "react";
import { runModels as viewedModels } from "./runSource";
import type { ModelCatalogState } from "./useModelCatalog";
import type { LevelRun } from "./useLevelRun";
import type { QuestionOverride } from "./useQuestionFields";

/**
 * While a Compare run is shown on a level page, the question form and model dropdowns mirror that run
 * (question, question_id, synthesis + judge from run_started). Mirroring never writes the stored model
 * preference: only a user change to a dropdown does (then it is a normal edit for the next run).
 */
export function useViewedRunInputs(run: LevelRun, models: ModelCatalogState): { readonly models: ModelCatalogState; readonly viewing: QuestionOverride | null } {
  const { compareView: cv, setCompareView, params } = run;
  const { synthesis, judge } = viewedModels(run);

  const shownModels = useMemo<ModelCatalogState>(() => {
    if (!cv || cv.modelsEdited || synthesis === null || judge === null) return models;
    return {
      ...models,
      selection: { synthesis, judge },
      setSelection: (sel) => {
        setCompareView({ ...cv, modelsEdited: true });
        models.setSelection(sel);
      },
    };
  }, [cv, synthesis, judge, models, setCompareView]);

  const viewing = useMemo<QuestionOverride | null>(() => {
    if (!cv || cv.formEdited || !params) return null;
    return {
      query: params.query,
      questionId: params.questionId ?? "",
      onEdit: () => setCompareView({ ...cv, formEdited: true }),
    };
  }, [cv, params, setCompareView]);

  return { models: shownModels, viewing };
}
