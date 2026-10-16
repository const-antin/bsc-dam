-- Analysis-only expansion of synthesized interfaces. The executable retains
-- its original packages, methods and schedule: no C++ hierarchy is flattened.
module DamHierarchy (HierarchyView(..), hierarchyView, hasHierarchyContexts) where

import ASyntax
import ASyntaxUtil (AExprs(..))
import SimPackage
import Pragma (PProp(..), RulePragma(..))
import Id (getIdString, getIdBaseString, getIdQualString, setIdQualString)
import VModInfo (vName, getVNameString)
import qualified Data.Map as M
import qualified Data.Set as S
import Data.List (nub)

-- Rule expressions remain package-local and must not be used for skipping
-- proofs. Only the resolved accesses/effects below are valid across packages.
data HierarchyView = HierarchyView {
    hvPackage :: SimPackage,
    hvUses :: [(AId,[(AId,String)])],
    hvEffects :: [AId],
    hvForeign :: [AId]
    }

hasHierarchyContexts :: SimSystem -> Bool
hasHierarchyContexts sys = any marked
    [p | p <- M.elems (ssys_packages sys), sp_name p /= ssys_top sys]
  where marked p = any context (sp_pps p)
        context PPdamContext = True
        context (PPdamScope _ _) = True
        context _ = False

qualify :: String -> AId -> AId
qualify "" i = i
qualify path i = setIdQualString i (join path (getIdQualString i))

join :: String -> String -> String
join "" b = b
join a "" = a
join a b = a ++ "." ++ b

-- Access summaries include method guards, argument expressions, returned
-- values and action bodies. Looking only at the exported method's name/type
-- would miss register-gated FIFO wrappers and methods with extra side effects.
data Access = Access (S.Set (AId,String)) Bool Bool [String]
empty :: Access
empty = Access S.empty False False []
combine :: [Access] -> Access
combine xs = Access (S.unions [s | Access s _ _ _ <- xs])
                    (or [b | Access _ b _ _ <- xs])
                    (or [b | Access _ _ b _ <- xs])
                    (concat [es | Access _ _ _ es <- xs])
bad :: String -> Access
bad s = Access S.empty False False [s]

hierarchyView :: SimSystem -> Either String HierarchyView
hierarchyView sys =
  case errors of
    e:_ -> Left e
    [] -> Right (HierarchyView view uses effects foreigns)
  where
    top = findPkg (ssys_packages sys) (ssys_top sys)
    packages = M.fromList [(getIdString (sp_name p),p) | p <- M.elems (ssys_packages sys)]
    sub v = M.lookup (getVNameString (vName (avi_vmi v))) packages
    -- Each node carries actual elaboration ownership inherited from ancestors.
    nodes = walk "" [] top
    walk path inherited p = (path,inherited,p) : concat
      [walk (join path (getIdString (avi_vname v)))
        (nub (inherited ++ rootOwner path p ++
          [scopeName path p n | PPdamScope n owned <- sp_pps p,
                                avi_vname v `elem` owned])) child
      | v <- M.elems (sp_state_instances p), Just child <- [sub v]] ++ concat
      [walk (join path name) (nub (inherited ++ rootOwner path p)) child
      | (name,modName) <- sp_noinline_instances p,
        Just child <- [M.lookup modName packages]]
    rootOwner path p = [path | PPdamContext `elem` sp_pps p]
    -- IExpand records the synthesized module's own annotation at its unnamed
    -- elaboration root. Its public identity is the actual instance path.
    scopeName path p n
      | PPdamContext `elem` sp_pps p && n == "_unnamed_" = path
      | otherwise = join path n
    primitives = [(qualify path (avi_vname v), v {avi_vname=qualify path (avi_vname v)})
                 | (path,_,p) <- nodes, v <- M.elems (sp_state_instances p),
                   Nothing <- [sub v]]
    moduleIds = [qualify path (avi_vname v) | (path,_,p) <- nodes,
                 v <- M.elems (sp_state_instances p), Just _ <- [sub v]]
    moduleIdMap = M.fromList [(getIdString i,i) | i <- moduleIds]
    -- Share subtree ownership lists instead of rescanning all primitives for
    -- every child of a large spatial hierarchy.
    stateBelow = M.fromList [(path, concat
      [let i = qualify path (avi_vname v) in i :
         case sub v of
           Nothing -> []
           Just _ -> M.findWithDefault [] (getIdString i) stateBelow
      | v <- M.elems (sp_state_instances p)]) | (path,_,p) <- nodes]
    ownedState path p owned = concat
      [let qualified = qualify path i in qualified :
         case sub v of
           Nothing -> []
           Just _ -> M.findWithDefault [] (getIdString qualified) stateBelow
      | i <- owned, Just v <- [M.lookup i (sp_state_instances p)]]
    scopes = [(scopeName path p n, ownedState path p owned ++
               [i | Just i <- [M.lookup path moduleIdMap],
                    PPdamContext `elem` sp_pps p, n == "_unnamed_"])
             | (path,_,p) <- nodes, PPdamScope n owned <- sp_pps p]
    ruleEntries = [(path,p,r,r {arule_id=qualify path (arule_id r),
                      arule_pragmas=map RPdamContext (nub (inherited ++ rootOwner path p ++
                        [scopeName path p n | RPdamContext n <- arule_pragmas r])) ++
                        [pr | pr <- arule_pragmas r, case pr of RPdamContext _ -> False; _ -> True]})
                 | (path,inherited,p) <- nodes, r <- sp_rules p]
    view = top {sp_state_instances=M.fromList primitives,
                sp_rules=[r | (_,_,_,r) <- ruleEntries],
                sp_pps=[PPdamScope n owned | (n,owned) <- scopes]}
    -- Lazy maps share expression and method closures across all callers. BSC
    -- has already rejected combinational cycles before this pass executes.
    summaries = M.fromList [(path,analyze path p) | (path,_,p) <- nodes]
    analyze path p = (ruleSummary, methods)
      where
        defs = M.map (\(ADef _ _ e _) -> expr e) (M.union (sp_local_defs p)
          (M.fromList [(i,d) | a <- sp_interface p,
            d@(ADef i _ _ _) <- case a of
              AIDef {aif_value=v} -> [v]
              AIActionValue {aif_value=v} -> [v]
              _ -> []]))
        methods = M.fromList [(getIdBaseString (aif_name i), iface i) | i <- sp_interface p]
        call i m = case M.lookup i (sp_state_instances p) of
          Nothing -> case lookup (getIdString i) (sp_noinline_instances p) of
            Just _ -> childCall i m
            Nothing -> bad ("unresolved DAM interface instance " ++ join path (getIdString i))
          Just v -> case sub v of
            Nothing -> Access (S.singleton (qualify path i,getIdBaseString m)) False False []
            Just _ -> childCall i m
        childCall i m = case M.lookup (join path (getIdString i)) summaries of
          Just (_,ms) -> ownEffects (qualify path i) $ M.findWithDefault
            (bad ("unresolved DAM interface method " ++ join path (getIdString i) ++ "." ++ getIdString m))
            (getIdBaseString m) ms
          Nothing -> bad "missing DAM hierarchy instance"
        -- Observable method bodies execute in their caller. They cannot be
        -- exported out of an annotated owner merely because they also touch
        -- a FIFO. A synthetic module resource makes the normal ownership/leak
        -- checks enforce that restriction without changing code generation.
        ownEffects i (Access uses effect foreignCall es) =
          Access (if effect then S.insert (i,"interface effect") uses else uses) effect foreignCall es
        expr e = case e of
          ASDef _ i -> M.findWithDefault (bad ("unresolved DAM definition " ++ join path (getIdString i))) i defs
          AMethCall _ i m xs -> combine (call i m : map expr xs)
          AMethValue _ i m -> call i m
          APrim _ _ _ xs -> combine (map expr xs)
          ATuple _ xs -> combine (map expr xs)
          ATupleSel _ x _ -> expr x
          AFunCall _ _ _ c xs -> combine (Access S.empty True c [] : map expr xs)
          ATaskValue _ _ _ c _ -> Access S.empty True c []
          ASPort {} -> empty -- method arguments/enables; data constructor ports rejected below
          ASParam {} -> empty
          ASInt {} -> empty
          ASStr {} -> empty
          ASReal {} -> empty
          _ -> bad ("unsupported expression in DAM interface hierarchy at " ++ path)
        action a = case a of
          ACall i m xs -> combine (call i m : map expr xs)
          AFCall {afcall_isC=c,aact_args=xs} -> combine (Access S.empty True c [] : map expr xs)
          ATaskAction {ataskact_isC=c,aact_args=xs} -> combine (Access S.empty True c [] : map expr xs)
        ruleSummary r = combine (expr (arule_pred r) : map action (arule_actions r) ++
          [bad "rule assumptions in synthesized DAM contexts are unsupported" | not (null (arule_assumps r))])
        iface i = combine (findAExprs (\e -> [expr e]) i ++
                    map action (concatMap arule_actions (aIfaceRules i)) ++
                    [bad "method assumptions in synthesized DAM contexts are unsupported" |
                     case i of AIDef {aif_assumps=as} -> not (null as); _ -> False])
    accesses = [(arule_id r',fst (summaries M.! path) r) | (path,_,r,r') <- ruleEntries]
    uses = [(r,S.toList s) | (r,Access s _ _ _) <- accesses]
    effects = [r | (r,Access _ True _ _) <- accesses]
    foreigns = [r | (r,Access _ _ True _) <- accesses]
    errors = ["data constructor ports in synthesized DAM hierarchy are unsupported; use FIFO interfaces (" ++ path ++ ")" |
              (path,_,p) <- nodes, any dataPort (sp_inputs p)] ++
             concat [es | (_,Access _ _ _ es) <- accesses]
    dataPort (AAI_Clock _ _) = False
    dataPort (AAI_Reset _) = False
    dataPort _ = True
