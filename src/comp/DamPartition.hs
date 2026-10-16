-- Experimental, deliberately restricted, cycle-preserving FIFO partition pass.
module DamPartition (damPartition, damInstrument, damHeader) where

import ASyntax
import ASyntaxUtil (AExprs(..))
import SimPackage
import SimCCBlock
import Id (getIdString, getIdBaseString)
import IntLit (ilValue)
import VModInfo (vName, getVNameString)
import qualified Data.Map as M
import qualified Data.Set as S
import Data.List (nub, sort, intercalate, isSuffixOf, isPrefixOf)
import Data.Maybe (isJust)
import Control.Monad (unless)
import System.Environment (lookupEnv)
import Control.Exception (Exception, throwIO, catch)
import Data.Typeable (Typeable)
import Text.Read (readMaybe)
import Prim (PrimOp(..))
import qualified DamPlan
import DamNecessity (wireRequirements)
import DamHierarchy
import Error (ErrorHandle)
import Flags (Flags(..))
import ForeignFunctions (mkFFDecl)
import PPrint (ppReadable)
import Pragma (PProp(..), RulePragma(..))

-- Necessary input conditions, not merely data dependencies. Conjunctions
-- combine requirements; disjunctions keep only requirements of both arms.
requiredInputs :: DefMap -> [AId] -> AExpr -> S.Set AId
requiredInputs defs inputs = req
  where
    memo = M.map (\(ADef _ _ e _) -> req e) defs
    req (ASDef _ i) = M.findWithDefault S.empty i memo
    req (AMethCall _ i m _) | i `elem` inputs && getIdBaseString m `elem` ["notEmpty","i_notEmpty"] = S.singleton i
    req (APrim _ (ATBit 1) op es) | op `elem` [PrimBAnd,PrimAnd] = S.unions (map req es)
    req (APrim _ (ATBit 1) op (e:es)) | op `elem` [PrimBOr,PrimOr] = foldl S.intersection (req e) (map req es)
    req (APrim _ _ PrimIf [_,a,b]) = S.intersection (req a) (req b)
    req _ = S.empty

-- Reject time, foreign functions, external ports and unknown expression
-- sources: a no-fire cycle is a fixed point only with stable local state.
stableExpressions :: DefMap -> ARule -> Bool
stableExpressions defs r = and (findAExprs (\e -> [stable e]) r)
  where
    memo = M.map (\(ADef _ _ e _) -> stable e) defs
    stable (ASDef _ i) = M.findWithDefault False i memo
    stable (APrim _ _ _ es) = all stable es
    stable (AMethCall _ _ _ es) = all stable es
    stable (AMethValue {}) = True
    stable (ATuple _ es) = all stable es
    stable (ATupleSel _ e _) = stable e
    stable (ASInt {}) = True
    stable (ASStr {}) = True
    stable (ASReal {}) = True
    stable _ = False

data Unsupported = Unsupported String deriving (Show, Typeable)
instance Exception Unsupported

require :: Bool -> String -> IO ()
require b msg = unless b (throwIO (Unsupported msg))

-- Follow the definition DAG, including foreign calls hidden in guards/values.
hasForeign :: DefMap -> ARule -> Bool
hasForeign defs r = or (findAExprs (\e -> [hasForeignExpr defs e]) r) ||
  any foreignAction (arule_actions r)
  where
    foreignAction (AFCall {afcall_isC=True}) = True
    foreignAction (ATaskAction {ataskact_isC=True}) = True
    foreignAction _ = False

hasForeignExpr :: DefMap -> AExpr -> Bool
hasForeignExpr defs = foreignExpr
  where
    memo = M.map (\(ADef _ _ e _) -> foreignExpr e) defs
    foreignExpr (ASDef _ i) = M.findWithDefault False i memo
    foreignExpr (AFunCall {ae_isC=True}) = True
    foreignExpr (ATaskValue {ae_isC=True}) = True
    foreignExpr (APrim _ _ _ es) = any foreignExpr es
    foreignExpr (AMethCall _ _ _ es) = any foreignExpr es
    foreignExpr (AFunCall _ _ _ _ es) = any foreignExpr es
    foreignExpr (ATuple _ es) = any foreignExpr es
    foreignExpr (ATupleSel _ e _) = foreignExpr e
    foreignExpr _ = False

-- Memoize dependency closures: wide rules can reference the same guard DAG
-- many times. Expanding every path separately is exponential. BSC's optimized
-- local combinational definitions are acyclic; Data.Map's lazy values share
-- each closure, and sets keep reconvergent dependencies unique.
ruleCalls :: DefMap -> ARule -> [(AId,String)]
ruleCalls defs r = S.toList $ S.unions
  (findAExprs (\e -> [uses e]) r ++
   [S.singleton (i,getIdBaseString m) | ACall i m _ <- arule_actions r])
  where
    memo = M.map (\(ADef _ _ rhs _) -> uses rhs) defs
    uses e = case e of
      ASDef _ i -> M.findWithDefault S.empty i memo
      AMethCall _ i m xs -> S.insert (i,getIdBaseString m) (S.unions (map uses xs))
      AMethValue _ i m -> S.singleton (i,getIdBaseString m)
      APrim _ _ _ xs -> S.unions (map uses xs)
      AFunCall _ _ _ _ xs -> S.unions (map uses xs)
      _ -> S.empty

-- Connected components of must-stay-together edges, NOT SCCs of FIFO traffic.
components :: [AId] -> [(AId,AId)] -> [[AId]]
components ids edges = collect (S.fromList ids)
  where
    adjacency = M.fromListWith S.union
      (concat [[(a,S.singleton b),(b,S.singleton a)] | (a,b) <- edges])
    visit seen pending
      | S.null pending = seen
      | otherwise = let (x,rest) = S.deleteFindMin pending
                        seen' = S.insert x seen
                        neighbors = M.findWithDefault S.empty x adjacency
                    in visit seen' (S.union rest (S.difference neighbors seen'))
    collect remaining
      | S.null remaining = []
      | otherwise = let group = visit S.empty (S.singleton (S.findMin remaining))
                    in S.toAscList group : collect (S.difference remaining group)

data Fifo = Fifo AId String Integer Integer
fifoId :: Fifo -> AId
fifoId (Fifo i _ _ _) = i

damPartition :: ErrorHandle -> Flags -> SimSystem -> IO SimSystem
damPartition errh flags sys = damPartitionChecked errh flags sys `catch` rejected
  where
    rejected (Unsupported reason) =
      ioError (userError ("DAM dam_context requirement failed: " ++ reason))

damPartitionChecked :: ErrorHandle -> Flags -> SimSystem -> IO SimSystem
damPartitionChecked errh flags sys = do
  output <- lookupEnv "BSC_DAM_MANIFEST"
  selected <- lookupEnv "BSC_DAM_PARTITION"
  case output of
    Nothing -> do
      require (not (isJust selected)) "BSC_DAM_PARTITION requires BSC_DAM_MANIFEST"
      return sys
    Just path -> do
      hierarchy <- if hasHierarchyContexts sys
        then case hierarchyView sys of
          Left reason -> throwIO (Unsupported reason)
          Right view -> return (Just view)
        else return Nothing
      let original = findPkg (ssys_packages sys) (ssys_top sys)
          pkg = maybe original hvPackage hierarchy
          schedules = ssys_schedules sys
          scopes = [(name,owned) | PPdamScope name owned <- sp_pps pkg]
          requested = map fst scopes
      require (PPdamContext `notElem` sp_pps original)
              "dam_context on the simulation top is unsupported; annotate ordinary child modules"
      require (not (null requested)) "-dam requires explicit dam_context annotations"
      require (length requested == length (nub requested)) "duplicate elaborated dam_context instance identity"
      require (length schedules == 1 && all ss_posedge schedules)
              "only one positive-edge clock is supported"
      require (all (null . ss_early_rules) schedules) "early/cross-clock rules are unsupported"
      require (not (or [hasForeignExpr (sp_local_defs p) e |
                        p <- M.elems (ssys_packages sys),
                        v <- M.elems (sp_state_instances p), e <- avi_iargs v]))
              "BDPI in module initialization is unsupported"
      require (null (sp_interface pkg)) "top-level methods are unsupported; use Empty"
      require (all (null . sp_gate_map) (M.elems (ssys_packages sys))) "gated clocks are unsupported"
      let insts = M.elems (sp_state_instances pkg)
          prim v = getVNameString (vName (avi_vmi v))
          registered = ["FIFO1", "FIFO10", "FIFO2", "FIFO20", "SizedFIFO", "SizedFIFO0"]
          -- CRegs implement SpecialFIFOs' same-cycle paths. Treat every port
          -- as shared state, never as a DAM cut. Loopy FIFO primitives also
          -- stay local: returned capacity can affect enq in this cycle.
          localOnly = ["CRegN5", "CRegUN5", "CRegA5", "FIFOL1", "FIFOL10",
                       "FIFOL2", "FIFOL20", "SizedFIFOL", "SizedFIFOL0"]
          wires = [avi_vname v | v <- insts, prim v `elem` ["RWire","RWire0"]]
          children = [avi_vname v | v <- insts,
                        prim v `elem` map (getIdString . sp_name) (M.elems (ssys_packages sys))]
          -- Register files are worker-local state. resource maps every port
          -- to the same instance, so readers and writers cannot be split.
          -- Keep clock skipping conservative for these primitives.
          supported = map (getIdString . sp_name) (M.elems (ssys_packages sys)) ++ registered ++ localOnly ++ ["RWire","RWire0"] ++ ["RegN", "RegUN", "RegA", "RegFile", "RegFileLoad"]
      mapM_ (\v -> require (prim v `elem` supported) ("unsupported primitive " ++ prim v))
        [v | p <- M.elems (ssys_packages sys), v <- M.elems (sp_state_instances p)]
      let fifos = [Fifo (avi_vname v) (prim v) w d |
                    v <- insts, prim v `elem` registered,
                    let raw = [ilValue n | ASInt _ _ n <- avi_iargs v],
                    let ns = if prim v `elem` ["FIFO10","FIFO20","SizedFIFO0"] then 0:raw else raw,
                    let w = head ns,
                    let d = case prim v of "FIFO1" -> 1; "FIFO10" -> 1; "FIFO2" -> 2; "FIFO20" -> 2; _ -> ns !! 1]
          fids = map fifoId fifos
          rs = sp_rules pkg
          ids = nub [getSchedNodeId node | s <- schedules, node <- ss_sched_order s]
          below child rid = (getIdString child ++ ".") `isPrefixOf` getIdString rid
          childRuleMap = M.fromList [(c,[r | r <- ids, below c r]) | c <- children]
          childUserMap = M.fromList [(c,[r | (r,us) <- uses, any ((== c) . fst) us]) | c <- children]
          childRules c = M.findWithDefault [] c childRuleMap
          childUsers c = M.findWithDefault [] c childUserMap
          star [] = []
          star (x:xs) = [(x,y) | y <- xs]
          hierarchyEdges = concat [star (childRules c ++ childUsers c) | c <- children]
          nested r = r `notElem` map arule_id rs
          -- All foreign calls and observable actions share one execution owner.
          -- Nested subtrees are atomic and conservatively join this owner when
          -- any foreign function exists in the design.
          localForeignRules = [arule_id r | r <- rs, hasForeign (sp_local_defs pkg) r] ++
                         [r | r <- ids, nested r, not (M.null (ssys_ffuncmap sys))]
          foreignRules = maybe localForeignRules hvForeign hierarchy
          foreignUsers = concat [childUsers c | c <- children, not (M.null (ssys_ffuncmap sys))]
          uses = maybe [(arule_id r, ruleCalls (sp_local_defs pkg) r) | r <- rs] hvUses hierarchy
          side m | m `elem` ["enq","notFull","i_notFull"] = "send"
                 | m `elem` ["deq","first","notEmpty","i_notEmpty"] = "receive"
                 | otherwise = "unsupported"
          resource (i,m) = (i, if i `elem` fids then side m else "state")
      require (not (null fifos)) "no supported FIFOs found"
      mapM_ (\(Fifo _ _ w d) -> require (w >= 0 && d > 0) "FIFO width must be nonnegative and depth positive") fifos
      require (all (\(_,us) -> all (\(i,m) -> i `notElem` fids || side m /= "unsupported") us) uses)
              ("FIFO clear or unsupported FIFO method: " ++ show [(getIdString i,m) | (_,us) <- uses, (i,m) <- us, i `elem` fids, side m == "unsupported"])
      require (all (null . arule_assumps) rs) "rule assumptions are unsupported"
      -- Preserve observable action ordering by retaining one owner.
      let effects = [(arule_id r,a) | r <- rs, a <- arule_actions r,
                                     case a of ACall {} -> False; _ -> True]
      require (all (\(_,a) -> case a of
                 AFCall {afcall_fun=f,afcall_isC=c} -> c || f == "$display"
                 ATaskAction {ataskact_isC=c} -> c
                 _ -> False) effects) "unsupported system task"
      -- Check nested system tasks too; a subtree cannot terminate one worker
      -- independently of the others. Calls inside methods are included.
      require (and [case a of
                  ACall {} -> True
                  AFCall {afcall_fun=f,afcall_isC=c} -> c || f == "$display"
                  ATaskAction {ataskact_isC=c} -> c
                  _ -> False
                | p <- M.elems (ssys_packages sys),
                  r <- sp_rules p ++ concatMap aIfaceRules (sp_interface p),
                  a <- arule_actions r]) "unsupported nested system task"
      obsoleteLayout <- lookupEnv "BSC_DAM_LAYOUT"
      require (not (isJust obsoleteLayout)) "BSC_DAM_LAYOUT is unsupported; use dam_context annotations"
      -- One star per resource has the same connected components as the
      -- quadratic clique of all rule pairs sharing it. These edges express
      -- co-ownership only; execution order still comes from BSC's schedule.
      let resourceUsers = M.fromListWith S.union
            [(resource u,S.singleton r) | (r,us) <- uses, u <- us]
          shared = concatMap (star . S.toAscList) (M.elems resourceUsers)
          conflicts = [(a,b) | s <- schedules, ASchedEsposito cs <- asch_scheduler (ss_schedule s),
                               (a,bs) <- cs, b <- bs]
          -- Residual schedule dependencies are also inseparable. This includes
          -- explicit execution ordering and any dependency not exposed as a
          -- primitive method use above. A registered FIFO alone has no such
          -- cross-rule edge after Bluesim schedule expansion.
          ordered = [(getSchedNodeId a,getSchedNodeId b) |
                      s <- schedules, (a,bs) <- ss_sched_graph s, b <- bs,
                      getSchedNodeId a /= getSchedNodeId b]
          nestedOwners = if isJust hierarchy then ids else concat [childRules c ++ childUsers c | c <- children]
          -- An explicit ordinary-module context owns synthesized instances
          -- allocated during its elaboration, including their nested rules.
          -- Unowned subtrees retain the conservative common owner. Subtrees
          -- with observable actions also join the observable-effect owner.
          childScopeMap = M.fromList [(c,[name | (name,owned) <- scopes, c `elem` owned]) | c <- children]
          childScopes c = M.findWithDefault [] c childScopeMap
          -- Index synthesized descendants once. Scanning the complete
          -- instance map per child is quadratic in replicated hierarchies;
          -- the map also contains every primitive register instance.
          packagesByName = M.fromList [(getIdString (sp_name p),p) |
                                      p <- M.elems (ssys_packages sys)]
          subtrees = M.fromListWith (++)
            [(takeWhile (/= '.') inst,[p]) |
             (inst,modName) <- M.toList (ssys_instmap sys), not (null inst),
             Just p <- [M.lookup modName packagesByName]]
          subtree c = M.findWithDefault [] (getIdString c) subtrees
          childEffects c = or [case a of ACall {} -> False; _ -> True
                              | p <- subtree c,
                                r <- sp_rules p ++ concatMap aIfaceRules (sp_interface p),
                                a <- arule_actions r]
          conservativeOwners = concat [childRules c ++ childUsers c | c <- children,
                                       length (childScopes c) /= 1 || childEffects c]
          effectRules = nub (foreignRules ++ foreignUsers ++ maybe (map fst effects) hvEffects hierarchy ++ conservativeOwners)
          -- Reuse membership lists in every ownership/leak check. Rebuilding
          -- descendant rule lists per outside rule makes large contexts cubic.
          contextRuleMap = M.fromList
            [(name,[arule_id r | r <- rs, RPdamContext name `elem` arule_pragmas r] ++
                   concat [childRules c | c <- children, name `elem` childScopes c]) | name <- requested]
          contextRules name = M.findWithDefault [] name contextRuleMap
          contextEdges = concatMap (star . contextRules) requested
          annotated = S.fromList (concatMap contextRules requested)
          mainRules = filter (`S.notMember` annotated) ids
          -- Unannotated logic is one main worker, never an inferred partition.
          mainEdges = star mainRules
          dependencyEdges = shared ++ conflicts ++ ordered ++ hierarchyEdges ++ star effectRules
          groups = components (sort ids) (dependencyEdges ++ contextEdges ++ mainEdges)
          owner r = head [n | (n,g) <- zip [0::Int ..] groups, r `elem` g]
          owners i which = nub [owner r | (r,us) <- uses, (j,m) <- us, i == j, side m == which]
          cuts = [(f,head a,head b) | f <- fifos, let i=fifoId f,
                    let a=owners i "send", let b=owners i "receive",
                    length a == 1, length b == 1, head a /= head b]
          jlist xs = "[" ++ intercalate "," xs ++ "]"
          inputIds n = [fifoId f | (f,_,b) <- cuts, b == n]
          cutIds = [fifoId f | (f,_,_) <- cuts]
          -- Safety is local to the group's resources. A SpecialFIFO in a
          -- different partition cannot invalidate this group's fixed point.
          -- The method whitelist below still rejects local CRegs/FIFOLs and
          -- RegFiles, whose observable read behavior needs a separate proof.
          safeGroup g = not (any (`elem` nestedOwners) g) && not (any (`elem` (foreignRules ++ foreignUsers)) g) &&
            all (stableExpressions (sp_local_defs pkg)) [r | r <- rs, arule_id r `elem` g] &&
            and [i `elem` cutIds || (i `elem` wires && m `elem` ["whas","wget","wset"]) || any (\v -> avi_vname v == i && prim v `elem` ["RegN","RegUN","RegA"] && m `elem` ["read","write"]) insts
                | (r,us) <- uses, r `elem` g, (i,m) <- us]
          reqFor proven n r = S.toList (S.union (requiredInputs (sp_local_defs pkg) (inputIds n) (arule_pred r)) (M.findWithDefault S.empty (arule_id r) proven))
          -- RegFileLoad has no autonomous behavior. Its readers/writers have
          -- already been joined by shared-state edges. Suppress redundant
          -- file loading in other workers, retaining an owner even for an
          -- unused memory so initialization diagnostics still happen once.
          memoryOwner i = case nub [owner r | (r,us) <- uses, any ((==i) . fst) us] of
                            [] -> Just 0
                            [n] -> Just n
                            _ -> Nothing
          unusedMemories n = [getIdString (avi_vname v) | v <- insts,
             prim v == "RegFileLoad", null children, M.null (ssys_ffuncmap sys),
             Just k <- [memoryOwner (avi_vname v)], k /= n]
          jgroup proven (n,g) = "{\"id\":" ++ show n ++ ",\"rules\":" ++ jlist (map (show . getIdString) g) ++
                        ",\"contains_foreign_calls\":" ++ (if any (`elem` (foreignRules ++ foreignUsers)) g then "true" else "false") ++
                        ",\"skip_safe\":" ++ (if safeGroup g then "true" else "false") ++
                        ",\"batch_safe\":" ++ (if any (`elem` (foreignRules ++ foreignUsers)) g then "false" else "true") ++
                        ",\"unused_memories\":" ++ jlist (map show (unusedMemories n)) ++
                        ",\"required_inputs\":" ++ jlist [jlist (map (show . getIdString) (reqFor proven n r)) | r <- rs, arule_id r `elem` g] ++
                        ",\"required_inputs_by_rule\":{" ++ intercalate "," [show (getIdString (arule_id r)) ++ ":" ++ jlist (map (show . getIdString) (reqFor proven n r)) | r <- rs, arule_id r `elem` g] ++ "}}"
          jcut (Fifo i p w d,a,b) = "{\"name\":" ++ show (getIdString i) ++ ",\"primitive\":" ++ show p ++
              ",\"width\":" ++ show w ++ ",\"depth\":" ++ show d ++ ",\"producer\":" ++ show a ++
              ",\"consumer\":" ++ show b ++ "}"
      -- A context is exact: validation may neither split it nor absorb rules.
      require (all (\r -> length [n | RPdamContext n <- arule_pragmas r] <= 1) rs)
              "nested or overlapping dam_context annotations cannot own the same rule"
      require (all ((<= 1) . length . childScopes) children)
              "nested or overlapping dam_context annotations cannot own the same synthesized child"
      mapM_ (\name -> do
        let members = contextRules name
            ownGroups = nub [owner r | r <- members]
        require (not (null members)) ("dam_context " ++ show name ++ " has no surviving rules")
        require (length ownGroups == 1) ("dam_context " ++ show name ++ " was split")
        let outsiders = [r | r <- groups !! head ownGroups, r `notElem` members]
        require (null outsiders) ("dam_context " ++ show name ++
          " cannot be isolated: shared state, scheduling, hierarchy or observable-effect dependency requires outside rules " ++
          unwords (map getIdString outsiders))) requested
      mapM_ (\(name,owned) -> do
        let leaks = [(r,i) | (r,us) <- uses, r `notElem` contextRules name,
                           (i,_) <- us, i `elem` owned, i `notElem` fids]
        require (null leaks) ("dam_context " ++ show name ++
          " exposes non-FIFO state to outside rules: " ++
          show [(getIdString r,getIdString i) | (r,i) <- nub leaks])) scopes
      let mainPartition = case mainRules of [] -> Nothing; r:_ -> Just (owner r)
          workerCount = length requested + if null mainRules then 0 else 1
      require (length groups == workerCount) "each worker must be an annotated context or the single main worker"
      require (damWorkers flags == 0 || workerCount <= damWorkers flags)
              ("dam_context requirements need " ++ show workerCount ++
               " workers (including main), exceeding -dam-workers " ++ show (damWorkers flags))
      require (length groups > 1 && not (null cuts)) "DAM requires at least two FIFO-connected workers"
      proven <- wireRequirements errh flags pkg wires
                  [(r,inputIds (owner (arule_id r))) | r <- rs,
                    safeGroup (groups !! owner (arule_id r))]
      DamPlan.writePlan (DamPlan.Plan
        (map (map getIdString) groups)
        [(getIdString i,w,d,a,b) | (Fifo i _ w d,a,b) <- cuts]
        [any (`elem` (foreignRules ++ foreignUsers)) g | g <- groups]
        [ppReadable (mkFFDecl f) | f <- M.elems (ssys_ffuncmap sys)]
        [(name,owner (head (contextRules name))) | name <- requested] mainPartition)
      writeFile path ("{\"schema\":3,\"protocol_version\":5,\"top\":" ++ show (getIdString (ssys_top sys)) ++
                      ",\"execution\":\"dam\",\"synthesis_packages\":" ++ show (M.size (ssys_packages sys)) ++
                      ",\"required_contexts\":" ++ jlist ["{\"instance\":" ++ show name ++ ",\"partition\":" ++ show (owner (head (contextRules name))) ++ "}" | name <- requested] ++
                      ",\"main_partition\":" ++ maybe "null" show mainPartition ++
                      ",\"partitions\":" ++ jlist (map (jgroup proven) (zip [0::Int ..] groups)) ++
                      ",\"channels\":" ++ jlist (map jcut cuts) ++
                      ",\"foreign_declarations\":" ++ jlist [show (ppReadable (mkFFDecl f)) | f <- M.elems (ssys_ffuncmap sys)] ++ "}\n")
      putStrLn ("DAM partition: " ++ show (length groups) ++ " partitions, " ++ show (length cuts) ++ " FIFO cuts")
      case selected of
        Nothing -> return sys
        Just value -> do
          n <- case readMaybe value of Just k -> return k; Nothing -> ioError (userError "invalid BSC_DAM_PARTITION")
          require (n >= 0 && n < length groups) "partition index out of range"
          let mine r = r `elem` (groups !! n)
              trim s = s { ss_sched_order = filter (mine . getSchedNodeId) (ss_sched_order s),
                           ss_sched_graph = [(a,filter (mine . getSchedNodeId) bs) | (a,bs) <- ss_sched_graph s, mine (getSchedNodeId a)] }
          putStrLn ("DAM worker " ++ show n ++ ": " ++ unwords (map getIdString (groups !! n)))
          return sys { ssys_schedules = map trim schedules }

-- Insert runtime cycle exchange in the compiler's C++ schedule IR after COpt.
-- This is deliberately after optimization: these calls are observable effects.
damInstrument :: [SimCCSched] -> IO [SimCCSched]
damInstrument schedules = do
  enabled <- lookupEnv "BSC_DAM_MANIFEST"
  let mark s@(SFSRuleExec _) = [s,SFSHostCall "dam_rule_fired"]
      mark (SFSCond c t f) = [SFSCond c (concatMap mark t) (concatMap mark f)]
      mark s = [s]
  let instrument s = s { sched_fn = (sched_fn s) {
        sf_body = SFSHostCall "dam_begin_cycle" : concatMap mark (sf_body (sched_fn s)) ++ [SFSHostCall "dam_end_cycle"] } }
  return (if isJust enabled then map instrument schedules else schedules)

damHeader :: String -> IO String
damHeader filename = do
  enabled <- lookupEnv "BSC_DAM_MANIFEST"
  return (if isJust enabled && ".cxx" `isSuffixOf` filename then "#include \"dam_runtime.h\"\n" else "")
