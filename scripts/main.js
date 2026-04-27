let fullData = [];
let filteredData = [];
let chart;

const filters = {
  party: document.getElementById("partyFilter"),
  chamber: document.getElementById("chamberFilter"),
  year: document.getElementById("yearFilter"),
  policy: document.getElementById("policyFilter")
};

const summary = document.getElementById("summary");
const previewTable = document.getElementById("previewTable");
const downloadBtn = document.getElementById("downloadBtn");

Papa.parse("data/data.csv", {
  download: true,
  header: true,
  complete: function(results) {
    console.log("CSV Loaded:");
    console.log(results.data);

    fullData = results.data.filter(row => row.speaker);
    filteredData = [...fullData];

    populateFilters();
    updateView();
  }
});

function getUniqueValues(column) {
  return [...new Set(
    fullData.map(row => row[column]).filter(Boolean)
  )].sort();
}

function populateSelect(selectElement, values) {
  values.forEach(value => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    selectElement.appendChild(option);
  });
}

function populateFilters() {
  populateSelect(filters.party, getUniqueValues("party"));
  populateSelect(filters.chamber, getUniqueValues("chamber"));
  populateSelect(filters.year, getUniqueValues("year"));
  populateSelect(filters.policy, getUniqueValues("policyclaiming"));

  Object.values(filters).forEach(filter => {
    filter.addEventListener("change", applyFilters);
  });

  downloadBtn.addEventListener("click", downloadCSV);
}
