library(tidyverse)
library(vegan)
library(indicspecies)
library(plotly)
library(readxl)
library(vegan)
library(DBI)
library(RSQLite)
library(topicmodels)
library(tidytext)

DATABASE <- ""
CSV_FILE <- ""

con <- dbConnect(RSQLite::SQLite(), DATABASE)
data <- dbReadTable(con, "presence_absence")
dbDisconnect(con)

plotnames <- data$plot_name
data$plot_name <- NULL
rownames(data) <- plotnames

library(slam)
dtm <- as.simple_triplet_matrix(as.matrix(data))

# fit LDA
lda_model <- LDA(dtm, k=12, method="VEM",
                 control=list(seed=2))

# plot-topic proportions (equivalent to cluster membership)
plot_topics <- posterior(lda_model)$topics

# species-topic distributions (which species define each community)
topic_species <- posterior(lda_model)$terms

# Get dominant topic per plot
dominant_topic <- apply(plot_topics, 1, which.max)

cluster_df <- data.frame(
  plot_id   = rownames(plot_topics),
  "1"=plot_topics[,1],
  "2"=plot_topics[,2],
  "3"=plot_topics[,3],
  "4"=plot_topics[,4],
  "5"=plot_topics[,5],
  "6"=plot_topics[,6],
  "7"=plot_topics[,7],
  "8"=plot_topics[,8],
  "9"=plot_topics[,9],
  "10"=plot_topics[,10],
  "11"=plot_topics[,11],
  "12"=plot_topics[,12]
)

write.csv(cluster_df, CSV_FILE, row.names=FALSE)

distBray <- vegdist(data,method="bray",binary=TRUE)
nmdsBray3d <- metaMDS(distBray,k=3)
hclustWard <- hclust(distBray,method="ward.D2")
grp <- cutree(hclustWard,k=12)

cluster_df <- data.frame(
  plot_id   = rownames(data),
  cluster = as.integer(grp)
)

write.csv(cluster_df, CSV_FILE, row.names = FALSE)

# Extract scores and cluster assignments
scores3d <- as.data.frame(scores(nmdsBray3d, display="sites"))
scores3d$cluster <- as.factor(grp)

plot_ly(scores3d,
  x=~NMDS1, y=~NMDS2, z=~NMDS3,
  color=~cluster,
  type="scatter3d",
  mode="markers",
  marker=list(size=4))

isa <- multipatt(data,grp,control=how(nperm=999))
summary(isa, alpha=0.05)

# Cluster colors matching plot_ly
cluster_colors <- c(
  "1"  = "rgba(102,194,165,1)",
  "2"  = "rgba(211,164,122,1)",
  "3"  = "rgba(229,147,127,1)",
  "4"  = "rgba(156,159,194,1)",
  "5"  = "rgba(194,150,199,1)",
  "6"  = "rgba(223,155,177,1)",
  "7"  = "rgba(182,204,108,1)",
  "8"  = "rgba(209,217,69,1)",
  "9"  = "rgba(253,215,61,1)",
  "10" = "rgba(237,202,125,1)",
  "11" = "rgba(212,190,160,1)",
  "12" = "rgba(179,179,179,1)"
)

# Convert rgba strings to hex for ggplot
rgba_to_hex <- function(rgba_str) {
  vals <- as.integer(regmatches(rgba_str, gregexpr("[0-9]+", rgba_str))[[1]])
  rgb(vals[1], vals[2], vals[3], maxColorValue = 255)
}
hex_colors <- sapply(cluster_colors, rgba_to_hex)

H <- sort(diversity(data, index = "shannon", groups = grp))
# Build data frame for plotting
plot_df <- data.frame(
  group     = factor(names(H), levels = names(H)),
  diversity = as.numeric(H)
)

# Plot
ggplot(plot_df, aes(x = group, y = diversity, fill = group)) +
  geom_bar(stat = "identity", width = 0.7) +
  scale_fill_manual(values = hex_colors) +
  geom_text(aes(label = group), vjust = -0.5, size = 3.5, family = "mono") +
  labs(
    title    = "Shannon Diversity by Cluster",
    x        = "Cluster (sorted by diversity)",
    y        = "Shannon H'"
  ) +
  theme_minimal(base_family = "mono") +
  theme(
    legend.position  = "none",
    panel.grid.major.x = element_blank(),
    axis.text.x      = element_blank(),
    plot.title       = element_text(face = "bold", size = 13),
    plot.subtitle    = element_text(size = 9, color = "grey50")
  )
