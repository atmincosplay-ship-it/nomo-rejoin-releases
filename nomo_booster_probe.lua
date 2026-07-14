--========================================================--
--                  NOMO BOOSTER PROBE
--                 v0.1 PHASE 1 SAFE
--========================================================--
-- PURPOSE:
--   Validate the real Grow a Garden pet schema before Booster mode is merged
--   into NOMO REJOIN or the stable v3.9 pet counter.
--
-- SAFETY:
--   - Does not teleport or rejoin.
--   - Does not call Cloudflare or any web endpoint.
--   - Does not modify <username>_state.json.
--   - Does not stop/replace NOMO PET COUNTER.
--   - Reads DataService once every 10 seconds by default.
--
-- OUTPUT:
--   nomo_rejoiner/<username>_booster_probe.json
--
-- VALUABLE RULE:
--   age >= 500
--   OR
--   truncated age-1 base weight >= 6.00 kg
--
-- AGE-1 BASE WEIGHT:
--   BaseWeight is treated as age-0.
--   age1 = BaseWeight * 1.1
--   displayed/compared after truncating to two decimals, never rounding up.
--========================================================--

local Players = game:GetService("Players")
local HttpService = game:GetService("HttpService")
local ReplicatedStorage = game:GetService("ReplicatedStorage")

local LocalPlayer = Players.LocalPlayer or Players.PlayerAdded:Wait()

-- Stop only an older Booster probe. The normal NOMO counter is untouched.
do
    local old = getgenv().NOMO_BOOSTER_PROBE
    if type(old) == "table" then
        old.Enabled = false
        old.Stop = true
    end
    task.wait(0.25)
end

getgenv().NOMO_BOOSTER_PROBE = getgenv().NOMO_BOOSTER_PROBE or {}
local Config = getgenv().NOMO_BOOSTER_PROBE

Config.Enabled = true
Config.Stop = false
Config.Version = "v0.1-phase1-safe"

Config.WriteFolder = Config.WriteFolder or "nomo_rejoiner"
Config.ScanEvery = math.max(5, tonumber(Config.ScanEvery or 10) or 10)
Config.MinAge = math.max(0, tonumber(Config.MinAge or 500) or 500)
Config.MinAge1BaseWeightKg = math.max(
    0,
    tonumber(Config.MinAge1BaseWeightKg or 6.00) or 6.00
)
Config.MaxValuablePets = math.max(
    1,
    math.floor(tonumber(Config.MaxValuablePets or 250) or 250)
)
Config.MaxSamplePets = math.max(
    1,
    math.floor(tonumber(Config.MaxSamplePets or 12) or 12)
)
Config.MaxObservedKeys = math.max(
    10,
    math.floor(tonumber(Config.MaxObservedKeys or 100) or 100)
)
Config.ExpectedPlaceId = tonumber(
    Config.ExpectedPlaceId or 126884695634066
) or 126884695634066
Config.OnlyScanExpectedPlace = Config.OnlyScanExpectedPlace ~= false
Config.Debug = Config.Debug == true

-- Optional username allow-list:
--   getgenv().NOMO_BOOSTER_PROBE.AllowedUsernames = {
--       ["nomoboost1"] = true,
--   }
-- Empty/nil means all usernames on the test device.
if type(Config.AllowedUsernames) ~= "table" then
    Config.AllowedUsernames = {}
end

local function sanitizeName(value)
    value = tostring(value or "")
    value = string.gsub(value, "[^%w_]", "_")
    if value == "" then
        value = "unknown"
    end
    return value
end

local safeUser = sanitizeName(LocalPlayer.Name)
Config.WriteFile = Config.WriteFile
    or (Config.WriteFolder .. "/" .. safeUser .. "_booster_probe.json")

local function usernameAllowed()
    if next(Config.AllowedUsernames) == nil then
        return true
    end
    return Config.AllowedUsernames[LocalPlayer.Name] == true
end

local function safeMakeFolder(folder)
    if type(writefile) ~= "function" then
        return false, "writefile unavailable"
    end

    pcall(function()
        if type(isfolder) == "function" then
            if not isfolder(folder) and type(makefolder) == "function" then
                makefolder(folder)
            end
        elseif type(makefolder) == "function" then
            makefolder(folder)
        end
    end)

    return true
end

local function safeWrite(path, text)
    if type(writefile) ~= "function" then
        return false, "writefile unavailable"
    end

    local tmp = path .. ".tmp"
    local ok, err = pcall(function()
        writefile(tmp, text)
    end)

    if not ok then
        local directOk, directErr = pcall(function()
            writefile(path, text)
        end)
        return directOk, directOk and nil or tostring(directErr or err)
    end

    local copied = pcall(function()
        local content = readfile(tmp)
        writefile(path, content)
    end)

    pcall(function()
        if type(delfile) == "function" then
            delfile(tmp)
        end
    end)

    if not copied then
        return false, "atomic copy failed"
    end
    return true
end

local MIN_VALID_EPOCH = 1600000000

local function realEpoch()
    local ok1, value1 = pcall(os.time)
    if ok1 and type(value1) == "number" and value1 >= MIN_VALID_EPOCH then
        return math.floor(value1)
    end

    local ok2, value2 = pcall(function()
        return DateTime.now().UnixTimestamp
    end)
    if ok2 and type(value2) == "number" and value2 >= MIN_VALID_EPOCH then
        return math.floor(value2)
    end

    local ok3, value3 = pcall(tick)
    if ok3 and type(value3) == "number" and value3 >= MIN_VALID_EPOCH then
        return math.floor(value3)
    end

    local ok4, value4 = pcall(function()
        return workspace:GetServerTimeNow()
    end)
    if ok4 and type(value4) == "number" and value4 >= MIN_VALID_EPOCH then
        return math.floor(value4)
    end

    return nil
end

local epochAnchor = realEpoch()
local clockAnchor = os.clock()

local function now()
    local current = realEpoch()
    if current then
        epochAnchor = current
        clockAnchor = os.clock()
        return current
    end

    if epochAnchor then
        return math.floor(
            epochAnchor + math.max(0, os.clock() - clockAnchor)
        )
    end

    return math.floor(MIN_VALID_EPOCH + os.clock())
end

local function toNumber(value)
    local number = tonumber(value)
    if type(number) ~= "number" then
        return nil
    end
    if number ~= number or number == math.huge or number == -math.huge then
        return nil
    end
    return number
end

local function truncateTwo(value)
    value = toNumber(value)
    if not value then
        return nil
    end
    return math.floor(value * 100) / 100
end

local function firstValue(containers, names)
    for _, container in ipairs(containers) do
        if type(container) == "table" then
            for _, name in ipairs(names) do
                local value = container[name]
                if value ~= nil then
                    return value, name
                end
            end
        end
    end
    return nil, ""
end

local function entryContainers(entry)
    if type(entry) ~= "table" then
        return {}
    end

    return {
        entry,
        entry.Data,
        entry.PetData,
        entry.Properties,
        entry.Info,
    }
end

local function scalarKeyList(entry, output)
    if type(entry) ~= "table" then
        return
    end

    for key, value in pairs(entry) do
        if type(key) == "string" then
            local valueType = type(value)
            if valueType ~= "function"
                and valueType ~= "thread"
                and valueType ~= "userdata" then
                output[key] = true
            end
        end
    end
end

local function countTable(value)
    local count = 0
    if type(value) == "table" then
        for _ in pairs(value) do
            count += 1
        end
    end
    return count
end

local function cleanString(value)
    if value == nil then
        return ""
    end
    return tostring(value)
end

local function parsePet(uuidKey, entry, observedKeys)
    if type(entry) ~= "table" then
        return nil, "entry_not_table"
    end

    scalarKeyList(entry, observedKeys)
    scalarKeyList(entry.Data, observedKeys)
    scalarKeyList(entry.PetData, observedKeys)

    local containers = entryContainers(entry)

    local uuidValue = firstValue(
        containers,
        {"UUID", "Uuid", "uuid", "PetUUID", "PET_UUID", "Id", "ID"}
    )
    local nameValue, nameField = firstValue(
        containers,
        {"PetType", "PetName", "Name", "Type", "Species"}
    )
    local ageValue, ageField = firstValue(
        containers,
        {"Level", "Age", "PetAge"}
    )
    local baseWeightValue, baseWeightField = firstValue(
        containers,
        {
            "BaseWeight",
            "BaseWeightKg",
            "BaseWeightKG",
            "BaseWeight_kg",
            "BaseWeightInKg",
        }
    )
    local mutationValue, mutationField = firstValue(
        containers,
        {"MutationType", "Mutation", "mutation"}
    )
    local hatchedFromValue = firstValue(
        containers,
        {"HatchedFrom", "EggType", "SourceEgg"}
    )
    local favoriteValue = firstValue(
        containers,
        {"IsFavorite", "Favorite", "Favorited"}
    )
    local boostsValue = firstValue(
        containers,
        {"Boosts", "PetBoosts"}
    )

    local uuid = cleanString(uuidValue)
    if uuid == "" then
        uuid = cleanString(uuidKey)
    end

    local name = cleanString(nameValue)
    local age = toNumber(ageValue)
    local baseWeightAge0 = toNumber(baseWeightValue)
    local baseWeightAge1 = nil

    if baseWeightAge0 then
        baseWeightAge0 = truncateTwo(baseWeightAge0)
        baseWeightAge1 = truncateTwo(baseWeightAge0 * 1.1)
    end

    local reasons = {}
    if age and age >= Config.MinAge then
        table.insert(reasons, "age")
    end
    if baseWeightAge1
        and baseWeightAge1 >= Config.MinAge1BaseWeightKg then
        table.insert(reasons, "weight")
    end

    return {
        uuid = uuid,
        name = name,
        age = age,
        base_weight_age0_kg = baseWeightAge0,
        base_weight_age1_kg = baseWeightAge1,
        mutation = cleanString(mutationValue),
        hatched_from = cleanString(hatchedFromValue),
        is_favorite = favoriteValue == true,
        boost_count = countTable(boostsValue),
        valuable = #reasons > 0,
        valuable_reasons = reasons,

        parsed_fields = {
            name = nameField,
            age = ageField,
            base_weight = baseWeightField,
            mutation = mutationField,
        },
    }, nil
end

local DataService = nil
local function getDataService()
    if DataService then
        return DataService
    end

    local ok, result = pcall(function()
        local modules = ReplicatedStorage:WaitForChild("Modules", 20)
        local module = modules and modules:WaitForChild("DataService", 10)
        return module and require(module)
    end)

    if ok and result then
        DataService = result
        return DataService
    end

    return nil
end

local function getPetInventory()
    local service = getDataService()
    if not service then
        return nil, "DataService unavailable"
    end

    local ok, data = pcall(function()
        return service:GetData()
    end)
    if not ok or type(data) ~= "table" then
        return nil, "DataService:GetData failed"
    end

    local petsData = data.PetsData
    local inventory = petsData and petsData.PetInventory
    local petData = inventory and inventory.Data

    if type(petData) ~= "table" then
        return nil, "PetsData.PetInventory.Data missing"
    end

    return petData, nil
end

local function sortParsedPets(items)
    table.sort(items, function(left, right)
        local leftWeight = tonumber(left.base_weight_age1_kg) or -1
        local rightWeight = tonumber(right.base_weight_age1_kg) or -1
        if leftWeight ~= rightWeight then
            return leftWeight > rightWeight
        end

        local leftAge = tonumber(left.age) or -1
        local rightAge = tonumber(right.age) or -1
        if leftAge ~= rightAge then
            return leftAge > rightAge
        end

        local leftName = string.lower(tostring(left.name or ""))
        local rightName = string.lower(tostring(right.name or ""))
        if leftName ~= rightName then
            return leftName < rightName
        end

        return tostring(left.uuid or "") < tostring(right.uuid or "")
    end)
end

local WRITE_SEQ = 0
local START_CLOCK = os.clock()
local lastState = nil

local function buildProbeState()
    WRITE_SEQ += 1

    local state = {
        probe_version = Config.Version,
        schema_version = 1,

        username = LocalPlayer.Name,
        display_name = LocalPlayer.DisplayName,
        place_id = tostring(game.PlaceId or ""),
        job_id = tostring(game.JobId or ""),
        expected_place_id = tostring(Config.ExpectedPlaceId),
        supported_place = tonumber(game.PlaceId) == Config.ExpectedPlaceId,

        min_age = Config.MinAge,
        min_age1_base_weight_kg = Config.MinAge1BaseWeightKg,
        weight_mode = "BaseWeight age-0 multiplied by 1.1",
        weight_precision = "truncate_2_decimals_no_rounding",

        ts = now(),
        write_seq = WRITE_SEQ,
        script_uptime = math.floor(math.max(0, os.clock() - START_CLOCK)),
        probe_alive = true,
        probe_ready = false,
        error = "",

        inventory_entry_count = 0,
        parsed_pet_count = 0,
        valuable_pet_count = 0,
        valuable_pets = {},
        sample_pets = {},
        observed_entry_keys = {},
        missing_fields = {
            name = 0,
            age = 0,
            base_weight = 0,
            mutation = 0,
            entry_not_table = 0,
        },
    }

    if not usernameAllowed() then
        state.error = "username not in AllowedUsernames"
        return state
    end

    if Config.OnlyScanExpectedPlace
        and tonumber(game.PlaceId) ~= Config.ExpectedPlaceId then
        state.error = "outside expected Booster place"
        return state
    end

    local petData, inventoryError = getPetInventory()
    if not petData then
        state.error = tostring(inventoryError or "pet inventory unavailable")
        return state
    end

    local parsed = {}
    local valuable = {}
    local observedKeys = {}

    for uuid, entry in pairs(petData) do
        state.inventory_entry_count += 1

        local pet, parseError = parsePet(uuid, entry, observedKeys)
        if not pet then
            if parseError == "entry_not_table" then
                state.missing_fields.entry_not_table += 1
            end
        else
            state.parsed_pet_count += 1

            if pet.name == "" then
                state.missing_fields.name += 1
            end
            if pet.age == nil then
                state.missing_fields.age += 1
            end
            if pet.base_weight_age0_kg == nil then
                state.missing_fields.base_weight += 1
            end
            if pet.mutation == "" then
                state.missing_fields.mutation += 1
            end

            table.insert(parsed, pet)
            if pet.valuable then
                table.insert(valuable, pet)
            end
        end
    end

    sortParsedPets(parsed)
    sortParsedPets(valuable)

    for index = 1, math.min(#parsed, Config.MaxSamplePets) do
        table.insert(state.sample_pets, parsed[index])
    end

    for index = 1, math.min(#valuable, Config.MaxValuablePets) do
        table.insert(state.valuable_pets, valuable[index])
    end

    local keys = {}
    for key in pairs(observedKeys) do
        table.insert(keys, key)
    end
    table.sort(keys)

    for index = 1, math.min(#keys, Config.MaxObservedKeys) do
        table.insert(state.observed_entry_keys, keys[index])
    end

    state.valuable_pet_count = #valuable
    state.valuable_pet_count_written = #state.valuable_pets
    state.valuable_pet_list_truncated = #valuable > #state.valuable_pets
    state.probe_ready = true
    return state
end

local function writeProbeState(state)
    safeMakeFolder(Config.WriteFolder)

    local okEncode, encoded = pcall(function()
        return HttpService:JSONEncode(state)
    end)
    if not okEncode then
        return false, tostring(encoded)
    end

    local okWrite, writeError = safeWrite(Config.WriteFile, encoded)
    if okWrite then
        lastState = state
    end
    return okWrite, writeError
end

local function snapshot()
    local state = buildProbeState()
    local ok, err = writeProbeState(state)

    if Config.Debug then
        if ok then
            print(
                "[NOMO BOOSTER PROBE] wrote "
                .. tostring(state.valuable_pet_count)
                .. " valuable / "
                .. tostring(state.parsed_pet_count)
                .. " parsed"
            )
        else
            warn("[NOMO BOOSTER PROBE] write failed:", tostring(err))
        end
    end

    return state, ok, err
end

getgenv().NOMO_BOOSTER_PROBE_STOP = function()
    Config.Enabled = false
    Config.Stop = true
    print("[NOMO BOOSTER PROBE] stop requested")
end

getgenv().NOMO_BOOSTER_PROBE_SNAPSHOT = function()
    local state, ok, err = snapshot()
    print(
        "[NOMO BOOSTER PROBE] snapshot:",
        ok and "written" or tostring(err)
    )
    return state
end

getgenv().NOMO_BOOSTER_PROBE_DUMP = function()
    local state = lastState or buildProbeState()
    print(HttpService:JSONEncode(state))
    return state
end

print("========================================")
print("[NOMO BOOSTER PROBE] " .. Config.Version)
print("[NOMO BOOSTER PROBE] account = " .. LocalPlayer.Name)
print("[NOMO BOOSTER PROBE] writing -> " .. Config.WriteFile)
print(
    "[NOMO BOOSTER PROBE] valuable = age >= "
    .. tostring(Config.MinAge)
    .. " OR age1 base >= "
    .. string.format("%.2f", Config.MinAge1BaseWeightKg)
    .. "kg"
)
print("[NOMO BOOSTER PROBE] production counter/state untouched")
print("========================================")

pcall(snapshot)

task.spawn(function()
    while Config.Enabled and not Config.Stop do
        task.wait(Config.ScanEvery)

        local ok, err = pcall(snapshot)
        if not ok then
            warn(
                "[NOMO BOOSTER PROBE] loop error:",
                tostring(err)
            )
        end
    end

    Config.Enabled = false
    Config.Stop = true
    print("[NOMO BOOSTER PROBE] stopped")
end)
